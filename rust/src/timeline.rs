//! Frame-clock scheduling and deterministic per-note material positions.
use crate::model::*;
use anyhow::{bail, Context, Result};
use rand::{rngs::StdRng, Rng, SeedableRng};
use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Clone)]
struct Quantized {
    id: usize,
    pitch: u8,
    first: u64,
    start: f64,
    offset: f64,
    velocity: u8,
}

pub fn build(data: &MidiData, fps: u32, chord: ChordPolicy) -> Result<Timeline> {
    if !(1..=120).contains(&fps) {
        bail!("帧率必须在 1–120 之间。")
    }
    if !data.duration.is_finite() || data.duration <= 0.0 || data.notes.is_empty() {
        bail!("所选 MIDI 没有可导出的音符或时长。")
    }
    let frame_count = (data.duration * fps as f64 - 1e-9).ceil().max(1.0);
    if frame_count > u64::MAX as f64 / 96_000.0 {
        bail!("MIDI 时长过长。")
    }
    let total_frames = frame_count as u64;
    let mut notes = BTreeMap::new();
    let mut boundaries: BTreeMap<u64, (Vec<usize>, Vec<usize>)> = BTreeMap::new();
    boundaries.entry(0).or_default();
    boundaries.entry(total_frames).or_default();
    for (id, note) in data.notes.iter().enumerate() {
        if note.pitch > 127
            || note.velocity == 0
            || note.velocity > 127
            || note.channel > 15
            || !note.start.is_finite()
            || !note.end.is_finite()
            || note.start < 0.0
            || note.end < note.start
            || !note.source_offset.is_finite()
            || note.source_offset < 0.0
        {
            bail!("MIDI 含有无效的音高、力度、时间或素材偏移。")
        }
        let first = (note.start * fps as f64 + 0.5)
            .floor()
            .clamp(0.0, total_frames as f64) as u64;
        let last = (note.end * fps as f64 + 0.5)
            .floor()
            .clamp(0.0, total_frames as f64) as u64;
        if last <= first {
            continue;
        }
        notes.insert(
            id,
            Quantized {
                id,
                pitch: note.pitch,
                first,
                start: note.start,
                offset: note.source_offset,
                velocity: note.velocity,
            },
        );
        boundaries.entry(first).or_default().1.push(id);
        boundaries.entry(last).or_default().0.push(id);
    }
    if notes.is_empty() {
        bail!("全部音符在当前帧率下都不足一帧，请提高帧率。")
    }
    let keys: Vec<_> = boundaries.keys().copied().collect();
    let mut active = BTreeSet::new();
    let mut segments: Vec<Segment> = Vec::new();
    let mut previous_ids = Vec::new();
    for pair in keys.windows(2) {
        let (first, last) = (pair[0], pair[1]);
        let (ending, starting) = &boundaries[&first];
        for id in ending {
            active.remove(id);
        }
        for id in starting {
            active.insert(*id);
        }
        let mut selected: Vec<usize> = active.iter().copied().collect();
        selected.sort_by(|a, b| {
            notes[a]
                .pitch
                .cmp(&notes[b].pitch)
                .then(notes[a].start.total_cmp(&notes[b].start))
                .then(a.cmp(b))
        });
        match chord {
            ChordPolicy::All => {}
            ChordPolicy::Highest => {
                if let Some(id) = selected.last().copied() {
                    selected = vec![id];
                }
            }
            ChordPolicy::Lowest => {
                if let Some(pitch) = selected.first().map(|id| notes[id].pitch) {
                    let id = selected
                        .iter()
                        .rev()
                        .find(|id| notes[*id].pitch == pitch)
                        .copied()
                        .unwrap();
                    selected = vec![id];
                }
            }
            ChordPolicy::Latest => {
                if let Some(id) = selected
                    .iter()
                    .max_by(|a, b| {
                        notes[*a]
                            .start
                            .total_cmp(&notes[*b].start)
                            .then(notes[*a].pitch.cmp(&notes[*b].pitch))
                            .then(a.cmp(b))
                    })
                    .copied()
                {
                    selected = vec![id];
                }
            }
        }
        if selected == previous_ids && !segments.is_empty() {
            segments.last_mut().unwrap().last = last;
        } else {
            let voices = selected
                .iter()
                .map(|id| {
                    let note = &notes[id];
                    Voice {
                        note_id: note.id,
                        pitch: note.pitch,
                        source_offset: note.offset + (first - note.first) as f64 / fps as f64,
                        velocity: note.velocity,
                    }
                })
                .collect();
            segments.push(Segment {
                first,
                last,
                voices,
            });
            previous_ids = selected;
        }
    }
    let mut voices: Vec<VoiceSpan> = Vec::new();
    let mut previous_by_id = HashMap::<usize, usize>::new();
    for segment in &segments {
        for voice in &segment.voices {
            if let Some(index) = previous_by_id.get(&voice.note_id).copied() {
                if voices[index].last_frame == segment.first {
                    voices[index].last_frame = segment.last;
                    continue;
                }
            }
            previous_by_id.insert(voice.note_id, voices.len());
            voices.push(VoiceSpan {
                note_id: voice.note_id,
                pitch: voice.pitch,
                first_frame: segment.first,
                last_frame: segment.last,
                source_offset: voice.source_offset,
                velocity: voice.velocity,
            });
        }
    }
    voices.sort_by_key(|voice| (voice.first_frame, voice.last_frame, voice.note_id));
    Ok(Timeline {
        total_frames,
        fps,
        segments,
        voices,
    })
}

pub fn plan_materials(
    data: &MidiData,
    options: &RenderOptions,
    media: &BTreeMap<u8, MediaInfo>,
) -> Result<(MidiData, BTreeMap<u8, SourceWindow>)> {
    for (pitch, choice) in &options.materials {
        if *pitch > 127
            || !choice.start.is_finite()
            || choice.start < 0.0
            || choice
                .end
                .is_some_and(|end| !end.is_finite() || end <= choice.start)
        {
            bail!("{} 的素材区间无效。", note_name(*pitch));
        }
    }
    let mut windows = BTreeMap::new();
    for (pitch, info) in media {
        if *pitch > 127 || !info.duration.is_finite() || info.duration <= 0.0 {
            bail!("{} 的素材时长无效。", note_name(*pitch));
        }
        let choice = options.materials.get(pitch).cloned().unwrap_or_default();
        let end = choice.end.unwrap_or(info.duration);
        if !(0.0 <= choice.start && choice.start < end && end <= info.duration + 1e-9) {
            bail!(
                "{} 的素材区间超出 0–{:.6} 秒范围。",
                note_name(*pitch),
                info.duration
            );
        }
        windows.insert(
            *pitch,
            SourceWindow {
                start: choice.start,
                end: end.min(info.duration),
            },
        );
    }
    let mut generator = StdRng::seed_from_u64(options.seed);
    let mut planned = data.clone();
    for note in &mut planned.notes {
        let window = windows
            .get(&note.pitch)
            .with_context(|| format!("缺少 {} 的素材时长。", note_name(note.pitch)))?;
        if !note.source_offset.is_finite()
            || note.source_offset < 0.0
            || !note.start.is_finite()
            || !note.end.is_finite()
            || note.end <= note.start
        {
            bail!("音符含有无效时间或素材续播偏移。")
        }
        if options
            .materials
            .get(&note.pitch)
            .is_some_and(|choice| choice.random)
        {
            let available =
                (window.duration() - (note.source_offset + note.end - note.start)).max(0.0);
            if available > 0.0 {
                note.source_offset += generator.gen_range(0.0..=available);
            }
        }
    }
    Ok((planned, windows))
}

pub fn sample_at_frame(frame: u64, fps: u32) -> u64 {
    ((frame as u128 * 48_000 * 2 + fps as u128) / (fps as u128 * 2)) as u64
}

#[cfg(test)]
mod tests {
    use super::*;
    fn data(notes: Vec<Note>, duration: f64) -> MidiData {
        MidiData {
            notes,
            tracks: vec![],
            duration,
            warnings: vec![],
        }
    }
    fn note(pitch: u8, start: f64, end: f64) -> Note {
        Note {
            pitch,
            start,
            end,
            velocity: 100,
            channel: 0,
            track: 0,
            source_offset: 0.0,
        }
    }
    #[test]
    fn overlapping_same_pitch_keeps_identity_and_offsets() {
        let song = data(vec![note(60, 0.0, 1.0), note(60, 0.5, 1.5)], 1.5);
        let timeline = build(&song, 30, ChordPolicy::All).unwrap();
        assert_eq!(timeline.segments[1].voices.len(), 2);
        assert_eq!(timeline.voices.len(), 2);
        assert!((timeline.segments[1].voices[0].source_offset - 0.5).abs() < 1e-9);
    }
    #[test]
    fn highest_resumes_underlying_long_note() {
        let song = data(vec![note(60, 0.0, 2.0), note(72, 0.5, 1.0)], 2.0);
        let timeline = build(&song, 30, ChordPolicy::Highest).unwrap();
        assert_eq!(timeline.segments[2].voices[0].pitch, 60);
        assert_eq!(timeline.segments[2].voices[0].source_offset, 1.0);
        assert_eq!(
            timeline
                .voices
                .iter()
                .filter(|voice| voice.pitch == 60)
                .count(),
            2
        );
    }
    #[test]
    fn absolute_clock_preserves_rests_and_fractional_tail() {
        let song = data(
            vec![note(60, 0.0333, 0.10001), note(62, 0.7001, 0.8001)],
            1.001,
        );
        let timeline = build(&song, 30, ChordPolicy::All).unwrap();
        assert_eq!(timeline.total_frames, 31);
        assert!(timeline.segments.first().unwrap().voices.is_empty());
        assert!(timeline.segments.last().unwrap().voices.is_empty());
        assert_eq!(sample_at_frame(31, 30), 49600);
    }
    #[test]
    fn zero_frame_notes_fail_clearly() {
        assert!(build(&data(vec![note(60, 0.0, 0.001)], 1.0), 30, ChordPolicy::All).is_err());
    }
    #[test]
    fn fixed_and_random_windows_preserve_crop_progress() {
        let mut song = data(vec![note(60, 0.0, 2.0), note(60, 2.0, 3.0)], 3.0);
        song.notes[0].source_offset = 0.5;
        let media = BTreeMap::from([(
            60,
            MediaInfo {
                path: "C4.wav".into(),
                duration: 5.0,
                has_audio: true,
                has_video: false,
            },
        )]);
        let mut options = RenderOptions::default();
        options.materials.insert(
            60,
            MaterialChoice {
                start: 1.0,
                end: Some(5.0),
                random: true,
            },
        );
        options.seed = 42;
        let (a, w) = plan_materials(&song, &options, &media).unwrap();
        let (b, _) = plan_materials(&song, &options, &media).unwrap();
        assert_eq!(a, b);
        assert_eq!(w[&60].start, 1.0);
        assert!(a.notes[0].source_offset >= 0.5 && a.notes[0].source_offset <= 2.0);
        options.seed = 43;
        let (c, _) = plan_materials(&song, &options, &media).unwrap();
        assert_ne!(a.notes, c.notes);
        assert_eq!(song.notes[0].source_offset, 0.5);
    }
    #[test]
    fn invalid_windows_and_missing_source_fail() {
        let song = data(vec![note(60, 0.0, 1.0)], 1.0);
        assert!(plan_materials(&song, &RenderOptions::default(), &BTreeMap::new()).is_err());
        let media = BTreeMap::from([(
            60,
            MediaInfo {
                path: "C4.wav".into(),
                duration: 5.0,
                has_audio: true,
                has_video: false,
            },
        )]);
        let mut options = RenderOptions::default();
        options.materials.insert(
            60,
            MaterialChoice {
                start: 4.0,
                end: Some(6.0),
                random: false,
            },
        );
        assert!(plan_materials(&song, &options, &media).is_err());
    }
}
