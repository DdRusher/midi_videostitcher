//! MIDI parsing on a global tempo clock, followed by non-destructive selection.
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::Path;

use anyhow::{bail, Context, Result};
use midly::{Format, MetaMessage, MidiMessage, Smf, Timing, TrackEventKind};

use crate::model::{MidiData, Note, Selection, TrackInfo};

#[derive(Clone, Copy)]
struct Start {
    pitch: u8,
    seconds: f64,
    velocity: u8,
    channel: u8,
    track: usize,
}

#[derive(Clone, Copy)]
enum Event {
    Tempo(u32),
    On(u8, u8, u8),
    Off(u8, u8),
    Controller(u8, u8, u8),
    Other,
}

fn finish(start: Start, end: f64, notes: &mut Vec<Note>, zero: &mut usize) {
    if end <= start.seconds {
        *zero += 1;
        return;
    }
    notes.push(Note {
        pitch: start.pitch,
        start: start.seconds,
        end,
        velocity: start.velocity,
        channel: start.channel,
        track: start.track,
        source_offset: 0.0,
    });
}

fn release_pedal(
    channel: u8,
    end: f64,
    sustained: &mut [Vec<Start>; 16],
    notes: &mut Vec<Note>,
    zero: &mut usize,
) {
    for start in sustained[channel as usize].drain(..) {
        finish(start, end, notes, zero);
    }
}

pub fn load(path: &Path) -> Result<MidiData> {
    load_with_sustain(path, false)
}

/// Load note intervals, optionally extending released keys with CC64 sustain.
/// The default `load` follows NoteOff so each clip stops when its key is released.
pub fn load_with_sustain(path: &Path, follow_sustain: bool) -> Result<MidiData> {
    let bytes =
        std::fs::read(path).with_context(|| format!("无法读取 MIDI 文件：{}", path.display()))?;
    parse_with_sustain(&bytes, follow_sustain)
        .with_context(|| format!("无法解析 MIDI 文件：{}", path.display()))
}

pub fn parse(bytes: &[u8]) -> Result<MidiData> {
    parse_with_sustain(bytes, false)
}

pub fn parse_with_sustain(bytes: &[u8], follow_sustain: bool) -> Result<MidiData> {
    let midi = Smf::parse(bytes).context("文件不是有效的 MIDI")?;
    if midi.header.format == Format::Sequential {
        bail!("暂不支持 Type 2 MIDI，请先转换为 Type 0 或 Type 1。")
    }
    let ticks_per_beat = match midi.header.timing {
        Timing::Metrical(ticks) if ticks.as_int() > 0 => ticks.as_int() as f64,
        _ => bail!("暂不支持 SMPTE 时间码 MIDI，请先转换为按拍计时。"),
    };
    let mut events = Vec::new();
    let mut tracks = Vec::with_capacity(midi.tracks.len());
    for (track_index, track) in midi.tracks.iter().enumerate() {
        let mut tick = 0_u64;
        let mut name = format!("轨道 {track_index}");
        let mut channels = BTreeSet::new();
        for (order, event) in track.iter().enumerate() {
            tick = tick
                .checked_add(event.delta.as_int() as u64)
                .context("MIDI 事件时间溢出")?;
            let kind = match event.kind {
                TrackEventKind::Meta(MetaMessage::Tempo(value)) => Event::Tempo(value.as_int()),
                TrackEventKind::Meta(MetaMessage::TrackName(value)) => {
                    let text = String::from_utf8_lossy(value).trim().to_owned();
                    if !text.is_empty() {
                        name = text;
                    }
                    Event::Other
                }
                TrackEventKind::Midi {
                    channel,
                    message: MidiMessage::NoteOn { key, vel },
                } if vel.as_int() > 0 => {
                    channels.insert(channel.as_int());
                    Event::On(channel.as_int(), key.as_int(), vel.as_int())
                }
                TrackEventKind::Midi {
                    channel,
                    message: MidiMessage::NoteOff { key, .. } | MidiMessage::NoteOn { key, .. },
                } => Event::Off(channel.as_int(), key.as_int()),
                TrackEventKind::Midi {
                    channel,
                    message: MidiMessage::Controller { controller, value },
                } => Event::Controller(channel.as_int(), controller.as_int(), value.as_int()),
                _ => Event::Other,
            };
            events.push((tick, track_index, order, kind));
        }
        tracks.push(TrackInfo {
            index: track_index,
            name,
            channels: channels.into_iter().collect(),
            note_count: 0,
        });
    }
    events.sort_unstable_by_key(|event| (event.0, event.1, event.2));
    let mut active: BTreeMap<(usize, u8, u8), VecDeque<Start>> = BTreeMap::new();
    let mut sustained: [Vec<Start>; 16] = std::array::from_fn(|_| Vec::new());
    let mut pedal = [false; 16];
    let (mut tempo, mut previous_tick, mut seconds) = (500_000_u32, 0_u64, 0.0);
    let (mut unmatched, mut zero, mut dangling) = (0, 0, 0);
    let mut notes = Vec::new();
    for (tick, track, _, event) in events {
        seconds += (tick - previous_tick) as f64 * tempo as f64 / ticks_per_beat / 1_000_000.0;
        previous_tick = tick;
        match event {
            Event::Tempo(value) => {
                if value == 0 {
                    bail!("MIDI 中包含无效的零速度值。")
                }
                tempo = value;
            }
            Event::On(channel, pitch, velocity) => {
                active
                    .entry((track, channel, pitch))
                    .or_default()
                    .push_back(Start {
                        pitch,
                        seconds,
                        velocity,
                        channel,
                        track,
                    });
            }
            Event::Off(channel, pitch) => {
                let key = (track, channel, pitch);
                let start = active.get_mut(&key).and_then(VecDeque::pop_front);
                if active.get(&key).is_some_and(VecDeque::is_empty) {
                    active.remove(&key);
                }
                if let Some(start) = start {
                    if follow_sustain && pedal[channel as usize] {
                        sustained[channel as usize].push(start);
                    } else {
                        finish(start, seconds, &mut notes, &mut zero);
                    }
                } else {
                    unmatched += 1;
                }
            }
            Event::Controller(channel, controller, value) => match controller {
                64 if follow_sustain => {
                    if value < 64 {
                        release_pedal(channel, seconds, &mut sustained, &mut notes, &mut zero);
                    }
                    pedal[channel as usize] = value >= 64;
                }
                120 | 123 => {
                    let keys: Vec<_> = active
                        .keys()
                        .filter(|key| key.1 == channel)
                        .copied()
                        .collect();
                    for key in keys {
                        if let Some(starts) = active.remove(&key) {
                            for start in starts {
                                if controller == 123 && follow_sustain && pedal[channel as usize] {
                                    sustained[channel as usize].push(start);
                                } else {
                                    finish(start, seconds, &mut notes, &mut zero);
                                }
                            }
                        }
                    }
                    if controller == 120 {
                        release_pedal(channel, seconds, &mut sustained, &mut notes, &mut zero);
                    }
                }
                121 => {
                    pedal[channel as usize] = false;
                    release_pedal(channel, seconds, &mut sustained, &mut notes, &mut zero);
                }
                _ => {}
            },
            Event::Other => {}
        }
    }
    for starts in active.into_values() {
        for start in starts {
            dangling += 1;
            finish(start, seconds, &mut notes, &mut zero);
        }
    }
    for starts in sustained {
        for start in starts {
            dangling += 1;
            finish(start, seconds, &mut notes, &mut zero);
        }
    }
    notes.sort_by(|a, b| {
        a.start
            .total_cmp(&b.start)
            .then(a.track.cmp(&b.track))
            .then(a.channel.cmp(&b.channel))
            .then(a.pitch.cmp(&b.pitch))
            .then(a.end.total_cmp(&b.end))
    });
    for note in &notes {
        tracks[note.track].note_count += 1;
    }
    let mut warnings = Vec::new();
    if unmatched > 0 {
        warnings.push(format!("已忽略 {unmatched} 个没有对应起音的结束事件。"));
    }
    if dangling > 0 {
        warnings.push(format!("{dangling} 个音符未正常结束，已在 MIDI 末尾关闭。"));
    }
    if zero > 0 {
        warnings.push(format!("已忽略 {zero} 个没有正持续时间的音符。"));
    }
    Ok(MidiData {
        notes,
        tracks,
        duration: seconds,
        warnings,
    })
}

pub fn select(data: &MidiData, selection: &Selection) -> Result<MidiData> {
    let end = selection.end.unwrap_or(data.duration);
    if !data.duration.is_finite()
        || data.duration <= 0.0
        || !selection.start.is_finite()
        || !end.is_finite()
        || selection.start < 0.0
        || selection.start >= end
        || end > data.duration + 1e-9
    {
        bail!("时间范围应满足 0 ≤ 开始时间 < 结束时间 ≤ MIDI 总时长。")
    }
    if let Some(tracks) = &selection.tracks {
        if tracks.is_empty()
            || tracks
                .iter()
                .any(|index| !data.tracks.iter().any(|track| track.index == *index))
        {
            bail!("请至少选择一个有效轨道。")
        }
    }
    if let Some(channels) = &selection.channels {
        if channels.is_empty() || channels.iter().any(|channel| *channel > 15) {
            bail!("请选择 1–16 范围内的通道。")
        }
    }
    let end = end.min(data.duration);
    let mut selected = data.clone();
    selected.duration = end - selection.start;
    selected.notes = data
        .notes
        .iter()
        .filter(|note| {
            selection
                .tracks
                .as_ref()
                .is_none_or(|tracks| tracks.contains(&note.track))
                && selection
                    .channels
                    .as_ref()
                    .is_none_or(|channels| channels.contains(&note.channel))
                && note.end > selection.start
                && note.start < end
        })
        .map(|note| {
            let mut note = note.clone();
            note.source_offset += (selection.start - note.start).max(0.0);
            note.start = note.start.max(selection.start) - selection.start;
            note.end = note.end.min(end) - selection.start;
            note
        })
        .collect();
    if selected.notes.is_empty() {
        bail!("当前轨道、通道和时间范围没有可用音符。")
    }
    Ok(selected)
}

pub fn parse_time(value: &str) -> Result<f64> {
    let fields: Vec<_> = value.trim().split(':').collect();
    if fields.is_empty() || fields.len() > 3 || fields.iter().any(|part| part.trim().is_empty()) {
        bail!("请输入秒数、分:秒或时:分:秒。")
    }
    let mut seconds = 0.0;
    for (index, field) in fields.iter().enumerate() {
        let number: f64 = field.trim().parse().context("时间必须是有效数字")?;
        if !number.is_finite()
            || number < 0.0
            || (fields.len() > 1 && index > 0 && number >= 60.0)
            || (index + 1 < fields.len() && number.fract() != 0.0)
        {
            bail!("时间格式无效；分、秒应小于 60，且不能为负数。")
        }
        seconds = seconds * 60.0 + number;
    }
    if !seconds.is_finite() {
        bail!("时间超出范围。")
    }
    Ok(seconds)
}

#[cfg(test)]
mod tests {
    use super::*;
    use midly::{Header, TrackEvent};
    fn file(tracks: Vec<Vec<(u32, TrackEventKind<'static>)>>) -> Vec<u8> {
        let midi = Smf {
            header: Header::new(
                if tracks.len() == 1 {
                    Format::SingleTrack
                } else {
                    Format::Parallel
                },
                Timing::Metrical(480.into()),
            ),
            tracks: tracks
                .into_iter()
                .map(|track| {
                    track
                        .into_iter()
                        .map(|(delta, kind)| TrackEvent {
                            delta: delta.into(),
                            kind,
                        })
                        .collect()
                })
                .collect(),
        };
        let mut bytes = Vec::new();
        midi.write_std(&mut bytes).unwrap();
        bytes
    }
    fn on(key: u8, vel: u8) -> TrackEventKind<'static> {
        TrackEventKind::Midi {
            channel: 0.into(),
            message: MidiMessage::NoteOn {
                key: key.into(),
                vel: vel.into(),
            },
        }
    }
    fn off(key: u8) -> TrackEventKind<'static> {
        TrackEventKind::Midi {
            channel: 0.into(),
            message: MidiMessage::NoteOff {
                key: key.into(),
                vel: 0.into(),
            },
        }
    }
    fn cc(controller: u8, value: u8) -> TrackEventKind<'static> {
        TrackEventKind::Midi {
            channel: 0.into(),
            message: MidiMessage::Controller {
                controller: controller.into(),
                value: value.into(),
            },
        }
    }
    fn end() -> TrackEventKind<'static> {
        TrackEventKind::Meta(MetaMessage::EndOfTrack)
    }
    #[test]
    fn global_tempo_and_trailing_rest() {
        let data = parse(&file(vec![
            vec![
                (
                    480,
                    TrackEventKind::Meta(MetaMessage::Tempo(1_000_000.into())),
                ),
                (960, end()),
            ],
            vec![(240, on(60, 90)), (720, off(60)), (480, end())],
        ]))
        .unwrap();
        assert!((data.notes[0].start - 0.25).abs() < 1e-9);
        assert!((data.notes[0].end - 1.5).abs() < 1e-9);
        assert!((data.duration - 2.5).abs() < 1e-9);
    }
    #[test]
    fn repeated_pitch_is_fifo_and_velocity_zero_releases() {
        let data = parse(&file(vec![vec![
            (0, on(60, 32)),
            (120, on(60, 127)),
            (120, on(60, 0)),
            (120, off(60)),
            (120, end()),
        ]]))
        .unwrap();
        assert_eq!(data.notes.len(), 2);
        assert_eq!(data.notes[0].velocity, 32);
        assert!((data.notes[0].end - 0.25).abs() < 1e-9);
        assert!((data.notes[1].end - 0.375).abs() < 1e-9);
    }
    #[test]
    fn pedal_in_another_track_and_sound_off() {
        let bytes = file(vec![
            vec![(0, cc(64, 127)), (720, cc(120, 0)), (240, end())],
            vec![
                (0, on(60, 90)),
                (240, off(60)),
                (0, on(64, 90)),
                (480, off(64)),
                (240, end()),
            ],
        ]);
        let data = parse_with_sustain(&bytes, true).unwrap();
        assert_eq!(data.notes.len(), 2);
        assert!((data.notes[0].end - 0.75).abs() < 1e-9);
        assert!((data.notes[1].end - 0.75).abs() < 1e-9);
        let direct = parse(&bytes).unwrap();
        assert_eq!(direct.notes[0].end, 0.25);
        assert_eq!(direct.notes[1].end, 0.75);
        assert_eq!(direct.tracks, data.tracks);
        assert_eq!(direct.duration, data.duration);
    }
    #[test]
    fn selection_retains_offset_rests_and_metadata() {
        let data = parse(&file(vec![vec![
            (240, on(60, 90)),
            (960, off(60)),
            (240, end()),
        ]]))
        .unwrap();
        let selected = select(
            &data,
            &Selection {
                start: 0.5,
                end: Some(1.5),
                ..Selection::default()
            },
        )
        .unwrap();
        assert_eq!(selected.tracks, data.tracks);
        assert_eq!(selected.duration, 1.0);
        assert_eq!(selected.notes[0].start, 0.0);
        assert!((selected.notes[0].end - 0.75).abs() < 1e-9);
        assert!((selected.notes[0].source_offset - 0.25).abs() < 1e-9);
        let second = select(
            &selected,
            &Selection {
                start: 0.1,
                end: Some(0.5),
                ..Selection::default()
            },
        )
        .unwrap();
        assert!((second.notes[0].source_offset - 0.35).abs() < 1e-9);
    }
    #[test]
    fn malformed_and_empty_selection_are_clear() {
        assert!(parse(b"bad").is_err());
        let data = parse(&file(vec![vec![
            (0, on(60, 100)),
            (480, off(60)),
            (480, end()),
        ]]))
        .unwrap();
        assert!(select(
            &data,
            &Selection {
                start: 0.6,
                end: Some(0.9),
                ..Selection::default()
            }
        )
        .is_err());
        assert!(select(
            &data,
            &Selection {
                tracks: Some(vec![]),
                ..Selection::default()
            }
        )
        .is_err());
        assert!(select(
            &data,
            &Selection {
                end: Some(3.0),
                ..Selection::default()
            }
        )
        .is_err());
    }
    #[test]
    fn same_pitch_on_separate_tracks_does_not_cross_pair() {
        let data = parse(&file(vec![
            vec![(0, on(60, 32)), (960, off(60)), (0, end())],
            vec![(240, on(60, 127)), (240, off(60)), (480, end())],
        ]))
        .unwrap();
        assert_eq!(data.notes.len(), 2);
        assert_eq!(data.notes[0].track, 0);
        assert_eq!(data.notes[0].end, 1.0);
        assert_eq!(data.notes[1].track, 1);
        assert_eq!(data.notes[1].end, 0.5);
        let selected = select(
            &data,
            &Selection {
                tracks: Some(vec![1, 1]),
                ..Selection::default()
            },
        )
        .unwrap();
        assert_eq!(selected.notes.len(), 1);
        assert_eq!(selected.duration, 1.0);
    }
    #[test]
    fn reset_controllers_releases_pedal_but_not_held_keys() {
        let bytes = file(vec![vec![
            (0, cc(64, 127)),
            (0, on(60, 90)),
            (0, on(64, 90)),
            (240, off(60)),
            (240, cc(121, 0)),
            (240, off(64)),
            (240, end()),
        ]]);
        let data = parse_with_sustain(&bytes, true).unwrap();
        assert_eq!(data.notes[0].end, 0.5);
        assert_eq!(data.notes[1].end, 0.75);
        let direct = parse(&bytes).unwrap();
        assert_eq!(direct.notes[0].end, 0.25);
        assert_eq!(direct.notes[1].end, 0.75);
    }
    #[test]
    fn all_notes_off_obeys_sustain() {
        let bytes = file(vec![vec![
            (0, cc(64, 127)),
            (0, on(60, 90)),
            (240, cc(123, 0)),
            (480, cc(64, 0)),
            (240, end()),
        ]]);
        let data = parse_with_sustain(&bytes, true).unwrap();
        assert_eq!(data.notes.len(), 1);
        assert_eq!(data.notes[0].end, 0.75);
        let direct = parse(&bytes).unwrap();
        assert_eq!(direct.notes.len(), 1);
        assert_eq!(direct.notes[0].end, 0.25);
    }
    #[test]
    fn default_note_off_closes_only_that_voice_at_the_overlap_boundary() {
        let bytes = file(vec![vec![
            (0, cc(64, 127)),
            (0, on(60, 90)),
            (240, on(62, 80)),
            (240, off(60)),
            (0, on(64, 70)),
            (240, on(62, 0)),
            (240, off(64)),
            (1440, cc(64, 0)),
            (0, end()),
        ]]);
        let direct = parse(&bytes).unwrap();
        assert_eq!(
            direct
                .notes
                .iter()
                .map(|n| (n.pitch, n.start, n.end))
                .collect::<Vec<_>>(),
            [(60, 0.0, 0.5), (62, 0.25, 0.75), (64, 0.5, 1.0)]
        );
        assert!(direct.warnings.is_empty());
        let schedule = crate::timeline::build(&direct, 20, crate::model::ChordPolicy::All).unwrap();
        let actual = schedule
            .segments
            .iter()
            .map(|s| {
                (
                    s.first,
                    s.last,
                    s.voices.iter().map(|v| v.pitch).collect::<Vec<_>>(),
                )
            })
            .collect::<Vec<_>>();
        assert_eq!(
            actual,
            [
                (0, 5, vec![60]),
                (5, 10, vec![60, 62]),
                (10, 15, vec![62, 64]),
                (15, 20, vec![64]),
                (20, 50, vec![]),
            ]
        );
        let sustained = parse_with_sustain(&bytes, true).unwrap();
        assert!(sustained.notes.iter().all(|n| n.end == 2.5));
    }
    #[test]
    fn ignored_unreleased_pedal_does_not_turn_closed_keys_into_dangling_notes() {
        let bytes = file(vec![vec![
            (0, cc(64, 127)),
            (0, on(60, 90)),
            (240, off(60)),
            (1680, end()),
        ]]);
        let direct = parse(&bytes).unwrap();
        assert_eq!(direct.notes[0].end, 0.25);
        assert_eq!(direct.duration, 2.0);
        assert!(direct.warnings.is_empty());
        let sustained = parse_with_sustain(&bytes, true).unwrap();
        assert_eq!(sustained.notes[0].end, 2.0);
        assert_eq!(sustained.warnings.len(), 1);
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("note-off.mid");
        std::fs::write(&path, &bytes).unwrap();
        assert_eq!(load(&path).unwrap().notes, direct.notes);
        assert_eq!(
            load_with_sustain(&path, true).unwrap().notes,
            sustained.notes
        );
    }
    #[test]
    fn explicit_sustain_preserves_non_pedal_note_intervals() {
        let bytes = file(vec![vec![
            (0, on(60, 64)),
            (120, on(60, 127)),
            (120, on(60, 0)),
            (120, off(60)),
            (120, end()),
        ]]);
        let direct = parse(&bytes).unwrap();
        let sustained = parse_with_sustain(&bytes, true).unwrap();
        assert_eq!(direct.notes, sustained.notes);
        assert_eq!(direct.tracks, sustained.tracks);
        assert_eq!(direct.duration, sustained.duration);
        assert_eq!(direct.warnings, sustained.warnings);
    }
    #[test]
    fn all_sound_off_closes_held_keys_in_both_timing_modes() {
        let bytes = file(vec![vec![
            (0, cc(64, 127)),
            (0, on(60, 90)),
            (0, on(64, 90)),
            (240, off(60)),
            (240, cc(120, 0)),
            (480, end()),
        ]]);
        let direct = parse(&bytes).unwrap();
        assert_eq!(
            direct.notes.iter().map(|n| n.end).collect::<Vec<_>>(),
            [0.25, 0.5]
        );
        let sustained = parse_with_sustain(&bytes, true).unwrap();
        assert_eq!(
            sustained.notes.iter().map(|n| n.end).collect::<Vec<_>>(),
            [0.5, 0.5]
        );
        assert!(direct.warnings.is_empty() && sustained.warnings.is_empty());
    }
    #[test]
    fn unmatched_dangling_and_zero_length_events_warn() {
        let data = parse(&file(vec![vec![
            (0, off(64)),
            (0, on(60, 90)),
            (0, off(60)),
            (0, on(67, 90)),
            (960, end()),
        ]]))
        .unwrap();
        assert_eq!(data.notes.len(), 1);
        assert_eq!(data.notes[0].pitch, 67);
        assert_eq!(data.notes[0].end, 1.0);
        assert_eq!(data.warnings.len(), 3);
    }
    #[test]
    fn sequential_and_timecode_files_are_rejected() {
        let mut bytes = file(vec![vec![(0, on(60, 90)), (480, off(60)), (0, end())]]);
        bytes[8] = 0;
        bytes[9] = 2;
        assert!(parse(&bytes).unwrap_err().to_string().contains("Type 2"));
        bytes[9] = 0;
        bytes[12] = 0xe7;
        bytes[13] = 40;
        assert!(parse(&bytes).unwrap_err().to_string().contains("SMPTE"));
    }
    #[test]
    fn time_formats_and_invalid_values() {
        assert_eq!(parse_time("1:02.5").unwrap(), 62.5);
        assert_eq!(parse_time("1:02:03").unwrap(), 3723.0);
        for bad in ["", "nan", "-1", "1:60", "1.2:03", "1::03", "1:02:03:04"] {
            assert!(parse_time(bad).is_err(), "{bad}");
        }
    }
}
