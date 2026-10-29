//! Real codec checks: the native compositor preserves voice identity and timing.
use midi_videostitcher_rust::model::*;
use midi_videostitcher_rust::{engine, media, midi, process, timeline};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

fn ffmpeg() -> Option<PathBuf> {
    media::find_ffmpeg(None).ok()
}
fn run(ffmpeg: &Path, values: &[&str], output: &Path) {
    let mut args: Vec<OsString> = process::args(&["-v", "error", "-nostdin", "-y"]);
    args.extend(values.iter().map(OsString::from));
    args.push(output.as_os_str().into());
    process::encode(ffmpeg, &args, None, &JobControl::default(), 0.0).unwrap();
}
fn asset(ffmpeg: &Path, path: &Path, first: &str, last: &str) {
    let graph=format!("color=c={first}:s=64x64:r=20:d=0.5[a];color=c={last}:s=64x64:r=20:d=0.5[b];[a][b]concat=n=2:v=1:a=0");
    run(
        ffmpeg,
        &[
            "-f",
            "lavfi",
            "-i",
            &graph,
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=400:sample_rate=48000:duration=1",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-r",
            "20",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            "-ac",
            "2",
        ],
        path,
    );
}
fn note(pitch: u8, start: f64, end: f64) -> Note {
    Note {
        pitch,
        start,
        end,
        velocity: 127,
        channel: 0,
        track: 0,
        source_offset: 0.0,
    }
}
fn song(notes: Vec<Note>, duration: f64) -> MidiData {
    MidiData {
        notes,
        tracks: vec![],
        duration,
        warnings: vec![],
    }
}
fn options(root: &Path, name: &str) -> RenderOptions {
    RenderOptions {
        video_dir: root.into(),
        output: root.join(name),
        width: 64,
        height: 64,
        fps: 20,
        visuals: VisualMode::Clips,
        ..Default::default()
    }
}
fn frame(ffmpeg: &Path, path: &Path, time: f64) -> Vec<u8> {
    let mut args = process::args(&["-v", "error", "-ss", &format!("{time:.9}"), "-i"]);
    args.push(path.as_os_str().into());
    args.extend(process::args(&[
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]));
    let output = process::run(ffmpeg, &args, None, &JobControl::default(), 0.0).unwrap();
    assert_eq!(
        output.code,
        0,
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}
fn pixel(frame: &[u8], x: usize, y: usize) -> [u8; 3] {
    let i = (y * 64 + x) * 3;
    frame[i..i + 3].try_into().unwrap()
}
fn blue(pixel: [u8; 3]) -> bool {
    pixel[2] > 150 && pixel[0] < 80 && pixel[1] < 80
}
fn red(pixel: [u8; 3]) -> bool {
    pixel[0] > 150 && pixel[1] < 80 && pixel[2] < 80
}

#[test]
fn native_grid_continues_material_across_layout_changes_and_loops() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    asset(&ffmpeg, &directory.path().join("C4.mkv"), "red", "blue");
    asset(&ffmpeg, &directory.path().join("E4.mkv"), "green", "green");
    let data = song(vec![note(60, 0.0, 1.4), note(64, 0.4, 0.8)], 1.6);
    let opts = options(directory.path(), "grid.mp4");
    let result = engine::render(&data, &opts, &JobControl::default()).unwrap();
    assert!(red(pixel(&frame(&ffmpeg, &result, 0.2), 32, 32)));
    assert!(blue(pixel(&frame(&ffmpeg, &result, 0.6), 16, 32)));
    assert!(blue(pixel(&frame(&ffmpeg, &result, 0.9), 32, 32)));
    assert!(red(pixel(&frame(&ffmpeg, &result, 1.2), 32, 32)));
    let rest = frame(&ffmpeg, &result, 1.5);
    assert!(rest.iter().all(|value| *value < 5));
}

#[test]
fn released_notes_leave_the_grid_and_audio_even_while_pedal_is_down() {
    use midly::{
        Format, Header, MetaMessage, MidiMessage, Smf, Timing, TrackEvent, TrackEventKind,
    };
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    asset(&ffmpeg, &directory.path().join("C4.mkv"), "red", "red");
    asset(&ffmpeg, &directory.path().join("E4.mkv"), "green", "green");
    let message = |message| TrackEventKind::Midi {
        channel: 0.into(),
        message,
    };
    let on = |pitch: u8| {
        message(MidiMessage::NoteOn {
            key: pitch.into(),
            vel: 100.into(),
        })
    };
    let off = |pitch: u8| {
        message(MidiMessage::NoteOff {
            key: pitch.into(),
            vel: 0.into(),
        })
    };
    let pedal = |value: u8| {
        message(MidiMessage::Controller {
            controller: 64.into(),
            value: value.into(),
        })
    };
    let events = vec![
        (0_u32, pedal(127)),
        (0, on(60)),
        (96, on(64)),
        (96, off(60)),
        (192, off(64)),
        (384, pedal(0)),
        (192, TrackEventKind::Meta(MetaMessage::EndOfTrack)),
    ];
    let file = Smf {
        header: Header {
            format: Format::SingleTrack,
            timing: Timing::Metrical(480.into()),
        },
        tracks: vec![events
            .into_iter()
            .map(|(delta, kind)| TrackEvent {
                delta: delta.into(),
                kind,
            })
            .collect()],
    };
    let mut bytes = Vec::new();
    file.write_std(&mut bytes).unwrap();
    let data = midi::parse(&bytes).unwrap();
    assert_eq!(
        data.notes
            .iter()
            .map(|n| (n.start, n.end))
            .collect::<Vec<_>>(),
        vec![(0.0, 0.2), (0.1, 0.4)]
    );
    let mut opts = options(directory.path(), "note-off.mp4");
    let output = engine::render(&data, &opts, &JobControl::default()).unwrap();
    assert!(red(pixel(&frame(&ffmpeg, &output, 0.05), 32, 32)));
    let overlap = frame(&ffmpeg, &output, 0.15);
    assert!(red(pixel(&overlap, 16, 32)));
    assert!(pixel(&overlap, 48, 32)[1] > 75);
    let remaining = pixel(&frame(&ffmpeg, &output, 0.25), 32, 32);
    assert!(remaining[1] > 75 && remaining[0] < 30 && remaining[2] < 30);
    assert!(frame(&ffmpeg, &output, 0.45).iter().all(|v| *v < 5));
    opts.output = directory.path().join("note-off.wav");
    let audio = engine::render_audio(&data, &opts, &JobControl::default()).unwrap();
    let samples: Vec<i16> = hound::WavReader::open(audio)
        .unwrap()
        .samples()
        .map(Result::unwrap)
        .collect();
    assert!(samples[..38400].iter().any(|v| v.abs() > 100));
    assert!(
        samples[38400..].iter().all(|v| v.abs() <= 1),
        "audio continued after the last NoteOff"
    );
}

#[test]
fn selected_window_does_not_read_outside_or_reset_same_pitch_voices() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    asset(&ffmpeg, &directory.path().join("C4.mkv"), "red", "blue");
    let data = song(vec![note(60, 0.0, 1.2), note(60, 0.2, 1.0)], 1.2);
    let mut opts = options(directory.path(), "selected.mp4");
    opts.materials.insert(
        60,
        MaterialChoice {
            start: 0.5,
            end: Some(1.0),
            random: false,
        },
    );
    let result = engine::render(&data, &opts, &JobControl::default()).unwrap();
    let output = frame(&ffmpeg, &result, 0.8);
    assert!(blue(pixel(&output, 16, 32)));
    assert!(blue(pixel(&output, 48, 32)));
}

#[test]
fn random_source_position_is_identical_for_planning_and_video() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    asset(&ffmpeg, &directory.path().join("C4.mkv"), "red", "blue");
    let data = song(vec![note(60, 0.0, 0.2)], 0.2);
    let mut opts = options(directory.path(), "random.mp4");
    opts.materials.insert(
        60,
        MaterialChoice {
            start: 0.0,
            end: None,
            random: true,
        },
    );
    let assets = engine::inspect_materials(&data, &opts, &JobControl::default()).unwrap();
    let mut chosen = None;
    for seed in 0..100 {
        opts.seed = seed;
        let (planned, _) = timeline::plan_materials(&data, &opts, &assets).unwrap();
        if planned.notes[0].source_offset > 0.60 {
            chosen = Some(planned.notes[0].source_offset);
            break;
        }
    }
    assert!(chosen.is_some());
    let result = engine::render(&data, &opts, &JobControl::default()).unwrap();
    assert!(blue(pixel(&frame(&ffmpeg, &result, 0.1), 32, 32)));
}

#[test]
fn imported_audio_exports_without_assets_and_preserves_exact_duration() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("music.wav");
    run(
        &ffmpeg,
        &[
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=800:sample_rate=48000:duration=1",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
        ],
        &source,
    );
    let data = song(vec![note(60, 0.0, 0.4)], 0.4);
    let mut opts = options(directory.path(), "music-cut.wav");
    opts.video_dir = directory.path().join("missing");
    opts.audio_path = Some(source);
    opts.audio_start = 0.8;
    assert!(
        engine::inspect_materials(&data, &opts, &JobControl::default())
            .unwrap()
            .is_empty()
    );
    let result = engine::render_audio(&data, &opts, &JobControl::default()).unwrap();
    let mut reader = hound::WavReader::open(result).unwrap();
    assert_eq!(reader.duration(), 19200);
    let samples: Vec<i16> = reader.samples().map(Result::unwrap).collect();
    assert!(samples[..9600].iter().any(|sample| sample.abs() > 100));
    assert!(samples[24000..].iter().all(|sample| sample.abs() < 3));
    opts.output = directory.path().join("music-cards.mp4");
    opts.visuals = VisualMode::Labels;
    let result = engine::render(&data, &opts, &JobControl::default()).unwrap();
    assert!(result.metadata().unwrap().len() > 1000);
}

#[test]
fn preflight_failure_retains_existing_output() {
    let directory = tempfile::tempdir().unwrap();
    let output = directory.path().join("old.mp4");
    fs::write(&output, b"original").unwrap();
    let data = song(vec![note(60, 0.0, 1.0)], 1.0);
    let mut opts = options(directory.path(), "old.mp4");
    opts.overwrite = true;
    assert!(engine::render(&data, &opts, &JobControl::default()).is_err());
    assert_eq!(fs::read(output).unwrap(), b"original");
}

#[test]
fn cancellation_stops_a_running_encoder_without_waiting_for_song_end() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let control = JobControl::default();
    let cancelled = control.cancel.clone();
    let started = Instant::now();
    let trigger = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(150));
        cancelled.store(true, Ordering::Relaxed);
    });
    let args = process::args(&[
        "-v",
        "error",
        "-nostdin",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=16x16:r=20",
        "-t",
        "60",
        "-f",
        "null",
        "-",
    ]);
    assert!(process::run(&ffmpeg, &args, None, &control, 60.0).is_err());
    trigger.join().unwrap();
    assert!(started.elapsed() < Duration::from_secs(5));
}
#[test]
fn card_video_retains_single_frame_notes_and_rests_at_high_fps() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    asset(&ffmpeg, &directory.path().join("C4.mkv"), "red", "blue");
    for fps in [24, 30, 60, 120] {
        let data = song(
            vec![
                note(60, 1.0 / fps as f64, 3.0 / fps as f64),
                note(60, 5.0 / fps as f64, 6.0 / fps as f64),
            ],
            8.0 / fps as f64,
        );
        let mut opts = options(directory.path(), &format!("cards-{fps}.mp4"));
        opts.fps = fps;
        opts.visuals = VisualMode::Labels;
        let result = engine::render(&data, &opts, &JobControl::default()).unwrap();
        let mut args = process::args(&["-v", "error", "-i"]);
        args.push(result.as_os_str().into());
        args.extend(process::args(&[
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ]));
        let output = process::run(&ffmpeg, &args, None, &JobControl::default(), 0.0).unwrap();
        assert_eq!(output.code, 0);
        let size = 64 * 64 * 3;
        assert_eq!(output.stdout.len(), size * 8, "fps {fps}");
        for (index, frame) in output.stdout.chunks_exact(size).enumerate() {
            let visible = frame.iter().map(|&value| value as u64).sum::<u64>() > size as u64 * 3;
            assert_eq!(
                visible,
                [1, 2, 5].contains(&index),
                "frame {index} at fps {fps}"
            );
        }
    }
}
#[test]
fn overlapping_same_pitch_keeps_independent_source_playheads() {
    let Some(ffmpeg) = ffmpeg() else { return };
    let directory = tempfile::tempdir().unwrap();
    asset(&ffmpeg, &directory.path().join("C4.mkv"), "red", "blue");
    let data = song(vec![note(60, 0.0, 1.0), note(60, 0.4, 1.0)], 1.0);
    let opts = options(directory.path(), "same-pitch.mp4");
    let output = engine::render(&data, &opts, &JobControl::default()).unwrap();
    let image = frame(&ffmpeg, &output, 0.6);
    assert!(blue(pixel(&image, 16, 32)));
    assert!(red(pixel(&image, 48, 32)));
}
