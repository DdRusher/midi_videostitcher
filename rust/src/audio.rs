//! Decode each source once, then mix disjoint output blocks in parallel.
//!
//! The active-note sweep avoids revisiting every note for every sample block.
//! A bounded PCM cache is shared by preview and export; long sources stay mmap'd.
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap};
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::UNIX_EPOCH;

use anyhow::{bail, Context, Result};
use memmap2::{Mmap, MmapOptions};
use rayon::prelude::*;

use crate::model::{JobControl, MediaInfo, RenderOptions, SourceWindow, Timeline};
use crate::{process, timeline::sample_at_frame};

const RATE: u64 = 48_000;
const CHANNELS: usize = 2;
const BLOCK: u64 = 16_384;
const CACHE_BYTES: usize = 256 * 1024 * 1024;
const MAX_CACHED_SOURCE: usize = 64 * 1024 * 1024;
// Remove sample discontinuities without extending a note beyond its NoteOff.
const DECLICK_FRAMES: u64 = RATE / 200; // 5 ms at 48 kHz.

#[derive(Default)]
struct DecodeCache {
    entries: HashMap<String, (Arc<Vec<f32>>, u64)>,
    bytes: usize,
    clock: u64,
}
static CACHE: OnceLock<Mutex<DecodeCache>> = OnceLock::new();

fn cache() -> &'static Mutex<DecodeCache> {
    CACHE.get_or_init(|| Mutex::new(DecodeCache::default()))
}
pub fn clear_cache() {
    let mut cache = cache().lock().unwrap_or_else(|error| error.into_inner());
    cache.entries.clear();
    cache.bytes = 0;
}

fn fingerprint(info: &MediaInfo, window: SourceWindow) -> Result<String> {
    let metadata = info.path.metadata()?;
    let modified = metadata
        .modified()?
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let canonical = info
        .path
        .canonicalize()
        .unwrap_or_else(|_| info.path.clone());
    let key = format!(
        "f32-stereo48k-v2|{}|{}|{}|{:x}|{:x}",
        canonical.display(),
        metadata.len(),
        modified,
        window.start.to_bits(),
        window.end.to_bits()
    );
    Ok(blake3::hash(key.as_bytes()).to_hex().to_string())
}

fn cached(key: &str) -> Option<Arc<Vec<f32>>> {
    let mut cache = cache().lock().unwrap_or_else(|error| error.into_inner());
    cache.clock = cache.clock.wrapping_add(1);
    let clock = cache.clock;
    cache.entries.get_mut(key).map(|entry| {
        entry.1 = clock;
        entry.0.clone()
    })
}

fn remember(key: String, samples: Arc<Vec<f32>>) {
    let bytes = samples.len() * 4;
    if bytes > MAX_CACHED_SOURCE {
        return;
    }
    let mut cache = cache().lock().unwrap_or_else(|error| error.into_inner());
    if let Some(old) = cache.entries.remove(&key) {
        cache.bytes -= old.0.len() * 4;
    }
    while cache.bytes + bytes > CACHE_BYTES {
        let oldest = cache
            .entries
            .iter()
            .min_by_key(|(_, entry)| entry.1)
            .map(|(key, _)| key.clone());
        if let Some(oldest) = oldest {
            if let Some(old) = cache.entries.remove(&oldest) {
                cache.bytes -= old.0.len() * 4;
            }
        } else {
            break;
        }
    }
    cache.clock = cache.clock.wrapping_add(1);
    let clock = cache.clock;
    cache.bytes += bytes;
    cache.entries.insert(key, (samples, clock));
}

enum Samples {
    Owned(Arc<Vec<f32>>),
    Mapped(Mmap),
}
impl Samples {
    fn values(&self) -> &[f32] {
        match self {
            Self::Owned(values) => values,
            Self::Mapped(mapping) => {
                // A read-only mmap is page-aligned, its checked length is a
                // multiple of four, and every f32 bit-pattern is valid. This
                // path is used only on little-endian hosts; others own values.
                unsafe {
                    std::slice::from_raw_parts(mapping.as_ptr().cast::<f32>(), mapping.len() / 4)
                }
            }
        }
    }
    fn frames(&self) -> usize {
        self.values().len() / CHANNELS
    }
}

fn read_samples(path: &Path, key: String) -> Result<Samples> {
    let file = File::open(path)?;
    let length = file.metadata()?.len();
    if length == 0 || length % 8 != 0 {
        bail!("素材选段没有完整的双声道音频样本：{}", path.display());
    }
    // File is private to this render and complete before it is mapped.
    let mapping = unsafe { MmapOptions::new().map(&file) }?;
    if length as usize <= MAX_CACHED_SOURCE || cfg!(target_endian = "big") {
        let values: Vec<f32> = mapping
            .chunks_exact(4)
            .map(|bytes| f32::from_le_bytes(bytes.try_into().unwrap()))
            .collect();
        if values.iter().any(|value| !value.is_finite()) {
            bail!("素材包含无效音频样本。");
        }
        let values = Arc::new(values);
        remember(key, values.clone());
        Ok(Samples::Owned(values))
    } else {
        Ok(Samples::Mapped(mapping))
    }
}

fn decode_sources(
    assets: &BTreeMap<u8, MediaInfo>,
    windows: &BTreeMap<u8, SourceWindow>,
    pitches: &BTreeSet<u8>,
    work: &Path,
    ffmpeg: &Path,
    control: &JobControl,
) -> Result<BTreeMap<u8, Samples>> {
    let mut decoded = BTreeMap::new();
    let mut pending = Vec::new();
    for pitch in pitches {
        control.check()?;
        let info = assets.get(pitch).context("找不到音符对应的素材")?;
        if !info.has_audio {
            continue;
        }
        let window = *windows.get(pitch).context("缺少素材选段设置")?;
        let key = fingerprint(info, window)?;
        if let Some(samples) = cached(&key) {
            decoded.insert(*pitch, Samples::Owned(samples));
        } else {
            pending.push((*pitch, info, window, key));
        }
    }
    let total = pitches.len().max(1);
    control.report(
        0.0,
        format!(
            "复用 {} 种已解码原声，读取 {} 种新素材…",
            decoded.len(),
            pending.len()
        ),
    );
    for (batch_index, batch) in pending.chunks(16).enumerate() {
        control.check()?;
        let mut arguments = process::ffmpeg_args();
        arguments.extend(process::args(&["-filter_threads", "1"]));
        for (_, info, window, _) in batch {
            arguments.extend(process::args(&[
                "-ss",
                &format!("{:.12}", window.start),
                "-threads",
                "1",
                "-i",
            ]));
            arguments.push(info.path.as_os_str().to_owned());
        }
        let mut paths = Vec::new();
        for (index, (pitch, _, window, key)) in batch.iter().enumerate() {
            let frames = (window.duration() * RATE as f64).round() as u64;
            if frames == 0 {
                bail!("素材选段短于一个音频采样。");
            }
            let destination = work.join(format!("source-{pitch:03}.f32"));
            let filters=format!("aresample={RATE}:async=1:first_pts=0,aformat=sample_fmts=flt:channel_layouts=stereo,atrim=end_sample={frames},asetpts=PTS-STARTPTS,apad,atrim=end_sample={frames}");
            arguments.extend(process::args(&[
                "-map",
                &format!("{index}:a:0"),
                "-vn",
                "-af",
                &filters,
                "-c:a",
                "pcm_f32le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-f",
                "f32le",
            ]));
            arguments.push(destination.as_os_str().to_owned());
            paths.push((*pitch, destination, key.clone()));
        }
        process::encode(ffmpeg, &arguments, None, &control.scoped(0.0, 0.0, ""), 0.0)?;
        for (pitch, path, key) in paths {
            decoded.insert(pitch, read_samples(&path, key)?);
        }
        control.report(
            0.20 * ((batch_index * 16 + batch.len()) as f64 / total as f64).min(1.0),
            format!("已读取 {} 种素材原声…", decoded.len()),
        );
    }
    Ok(decoded)
}

#[derive(Clone, Debug)]
struct Interval {
    first: u64,
    last: u64,
    offset: u64,
    source: usize,
    gain: f32,
}
#[derive(Clone, Debug)]
struct BlockTask {
    first: u64,
    frames: usize,
    active: Vec<usize>,
}

fn note_fade_frames(length: u64) -> u64 {
    DECLICK_FRAMES.min(length.saturating_sub(1) / 2)
}

fn loop_fade_frames(period: u64) -> u64 {
    // Very short windows still retain an unchanged middle portion.
    DECLICK_FRAMES.min(period.saturating_sub(1) / 4)
}

fn edge_gain(position: u64, length: u64, fade: u64) -> f32 {
    if fade == 0 {
        return 1.0;
    }
    let distance = position.min(length - 1 - position);
    if distance >= fade {
        return 1.0;
    }
    let x = distance as f32 / fade as f32;
    x * x * (3.0 - 2.0 * x)
}

fn edge_chunk(position: u64, length: u64, fade: u64, limit: u64) -> u64 {
    if fade == 0 {
        return limit;
    }
    if position < fade {
        limit.min(fade - position)
    } else if position < length - 1 - fade {
        limit.min(length - 1 - fade - position)
    } else {
        limit
    }
}

fn mix_block(
    task: &BlockTask,
    intervals: &[Interval],
    sources: &[Samples],
    control: &JobControl,
) -> Result<Vec<f32>> {
    control.check()?;
    let mut output = vec![0.0_f32; task.frames * CHANNELS];
    let end = task.first + task.frames as u64;
    for index in &task.active {
        let interval = &intervals[*index];
        let mut target = interval.first.max(task.first);
        let last = interval.last.min(end);
        if last <= target {
            continue;
        }
        let source = &sources[interval.source];
        let samples = source.values();
        let period = source.frames() as u64;
        let note_length = interval.last - interval.first;
        let note_fade = note_fade_frames(note_length);
        let loop_fade = loop_fade_frames(period);
        let mut source_frame = (interval.offset + (target - interval.first)) % period;
        while target < last {
            control.check()?;
            let age = target - interval.first;
            let count = edge_chunk(
                age,
                note_length,
                note_fade,
                edge_chunk(
                    source_frame,
                    period,
                    loop_fade,
                    (last - target).min(period - source_frame),
                ),
            ) as usize;
            let output_start = (target - task.first) as usize * CHANNELS;
            let source_start = source_frame as usize * CHANNELS;
            let destination = &mut output[output_start..output_start + count * CHANNELS];
            let input = &samples[source_start..source_start + count * CHANNELS];
            let flat = edge_gain(age, note_length, note_fade) == 1.0
                && edge_gain(age + count as u64 - 1, note_length, note_fade) == 1.0
                && edge_gain(source_frame, period, loop_fade) == 1.0
                && edge_gain(source_frame + count as u64 - 1, period, loop_fade) == 1.0;
            if flat {
                // The unchanged middle retains the vectorized multiply-add.
                // Threads never share a block or reorder the active voices.
                for (destination, input) in destination.iter_mut().zip(input) {
                    *destination += *input * interval.gain;
                }
            } else {
                for (frame, (destination, input)) in destination
                    .chunks_exact_mut(CHANNELS)
                    .zip(input.chunks_exact(CHANNELS))
                    .enumerate()
                {
                    // Absolute voice/sample positions make fades independent
                    // of worker blocks and video-grid layout changes.
                    let gain = interval.gain
                        * edge_gain(age + frame as u64, note_length, note_fade)
                        * edge_gain(source_frame + frame as u64, period, loop_fade);
                    for channel in 0..CHANNELS {
                        destination[channel] += input[channel] * gain;
                    }
                }
            }
            target += count as u64;
            source_frame = (source_frame + count as u64) % period;
        }
    }
    Ok(output)
}

fn write_float(writer: &mut impl Write, values: &[f32]) -> Result<()> {
    if cfg!(target_endian = "little") {
        // f32 has no padding and the slice remains borrowed during the write.
        let bytes = unsafe {
            std::slice::from_raw_parts(values.as_ptr().cast::<u8>(), std::mem::size_of_val(values))
        };
        writer.write_all(bytes)?;
    } else {
        for value in values {
            writer.write_all(&value.to_le_bytes())?;
        }
    }
    Ok(())
}

fn write_mix(
    intervals: &[Interval],
    sources: &[Samples],
    total: u64,
    path: &Path,
    workers: usize,
    control: &JobControl,
) -> Result<()> {
    control.check()?;
    let requested = if workers == 0 {
        std::thread::available_parallelism()
            .map(usize::from)
            .unwrap_or(2)
            .min(16)
    } else {
        workers.clamp(1, 32)
    };
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(requested)
        .build()?;
    let mut writer = BufWriter::with_capacity(1024 * 1024, File::create(path)?);
    let mut starts: Vec<usize> = (0..intervals.len()).collect();
    starts.sort_by_key(|index| (intervals[*index].first, *index));
    let mut active = BTreeSet::new();
    let mut ending = BinaryHeap::<Reverse<(u64, usize)>>::new();
    let mut cursor = 0;
    let mut first = 0;
    while first < total {
        control.check()?;
        let mut tasks = Vec::with_capacity(requested * 4);
        for _ in 0..requested * 4 {
            if first >= total {
                break;
            }
            let last = (first + BLOCK).min(total);
            while let Some(Reverse((last_sample, index))) = ending.peek().copied() {
                if last_sample > first {
                    break;
                }
                ending.pop();
                active.remove(&index);
            }
            while cursor < starts.len() && intervals[starts[cursor]].first < last {
                let index = starts[cursor];
                cursor += 1;
                if intervals[index].last > first {
                    active.insert(index);
                    ending.push(Reverse((intervals[index].last, index)));
                }
            }
            tasks.push(BlockTask {
                first,
                frames: (last - first) as usize,
                active: active.iter().copied().collect(),
            });
            first = last;
        }
        let outputs: Vec<Result<Vec<f32>>> = pool.install(|| {
            tasks
                .par_iter()
                .map(|task| mix_block(task, intervals, sources, control))
                .collect()
        });
        for output in outputs {
            control.check()?;
            write_float(&mut writer, &output?)?;
        }
        control.report(
            0.20 + 0.65 * first as f64 / total as f64,
            "Rust 正在分块混合音符原声…",
        );
    }
    writer.flush()?;
    Ok(())
}

pub fn render_mix(
    timeline: &Timeline,
    assets: &BTreeMap<u8, MediaInfo>,
    windows: &BTreeMap<u8, SourceWindow>,
    options: &RenderOptions,
    work: &Path,
    ffmpeg: &Path,
    control: &JobControl,
) -> Result<PathBuf> {
    control.check()?;
    let total = sample_at_frame(timeline.total_frames, timeline.fps);
    if total == 0 {
        bail!("选区没有可导出的音频时长。");
    }
    let destination = work.join("mixed-audio.wav");
    let mut arguments = process::ffmpeg_args();
    if let Some(source) = &options.audio_path {
        if !options.audio_start.is_finite() || options.audio_start < 0.0 {
            bail!("导入音频起点必须是有限的非负秒数。");
        }
        arguments.extend(process::args(&[
            "-ss",
            &format!("{:.12}", options.audio_start),
            "-i",
        ]));
        arguments.push(source.as_os_str().to_owned());
        let filter=format!("aresample={RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,apad,atrim=end_sample={total},alimiter=limit=0.95:level=false:latency=true:attack=5:release=50,apad,atrim=end_sample={total}");
        arguments.extend(process::args(&[
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            &filter,
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-rf64",
            "auto",
        ]));
        arguments.push(destination.as_os_str().to_owned());
        process::encode(ffmpeg, &arguments, None, control, timeline.seconds())?;
        return Ok(destination);
    }
    let pitches: BTreeSet<_> = timeline
        .voices
        .iter()
        .filter(|voice| {
            assets
                .get(&voice.pitch)
                .is_some_and(|asset| asset.has_audio)
        })
        .map(|voice| voice.pitch)
        .collect();
    if pitches.is_empty() {
        arguments.extend(process::args(&[
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            &format!("{:.12}", total as f64 / RATE as f64),
            "-c:a",
            "pcm_s16le",
            "-rf64",
            "auto",
        ]));
        arguments.push(destination.as_os_str().to_owned());
        process::encode(ffmpeg, &arguments, None, control, timeline.seconds())?;
        return Ok(destination);
    }
    let decoded = decode_sources(assets, windows, &pitches, work, ffmpeg, control)?;
    let mut source_indices = BTreeMap::new();
    let mut sources = Vec::new();
    for (pitch, source) in decoded {
        source_indices.insert(pitch, sources.len());
        sources.push(source);
    }
    let mut intervals = Vec::new();
    for voice in &timeline.voices {
        if let Some(source) = source_indices.get(&voice.pitch).copied() {
            let first = sample_at_frame(voice.first_frame, timeline.fps);
            let last = sample_at_frame(voice.last_frame, timeline.fps);
            if last <= first {
                continue;
            }
            let period = sources[source].frames() as u64;
            let offset = ((voice.source_offset % (period as f64 / RATE as f64)) * RATE as f64)
                .round() as u64
                % period;
            intervals.push(Interval {
                first,
                last,
                offset,
                source,
                gain: if options.velocity {
                    voice.velocity as f32 / 127.0
                } else {
                    1.0
                },
            });
        }
    }
    let mixed = work.join("mixed-audio.f32");
    write_mix(
        &intervals,
        &sources,
        total,
        &mixed,
        options.workers,
        control,
    )?;
    // Release mmap handles before the enclosing temporary directory is removed.
    drop(sources);
    arguments.extend(process::args(&[
        "-f", "f32le", "-ar", "48000", "-ac", "2", "-i",
    ]));
    arguments.push(mixed.as_os_str().to_owned());
    let filters=format!("alimiter=limit=0.95:level=false:latency=true:attack=5:release=50,apad,atrim=end_sample={total}");
    arguments.extend(process::args(&[
        "-vn",
        "-af",
        &filters,
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-rf64",
        "auto",
    ]));
    arguments.push(destination.as_os_str().to_owned());
    process::encode(
        ffmpeg,
        &arguments,
        None,
        &control.scoped(0.85, 1.0, "正在限制混音峰值"),
        timeline.seconds(),
    )?;
    Ok(destination)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{MaterialChoice, MidiData, Note};
    fn samples(values: &[f32]) -> Samples {
        Samples::Owned(Arc::new(values.to_vec()))
    }
    fn interval(first: u64, last: u64, offset: u64, gain: f32) -> Interval {
        Interval {
            first,
            last,
            offset,
            source: 0,
            gain,
        }
    }
    #[test]
    fn parallel_blocks_equal_scalar_reference_with_overlap_and_loop() {
        let source: Vec<f32> = (0..50)
            .flat_map(|index| {
                let value = (index as f32 * 0.3).sin() * 0.1;
                [value, value]
            })
            .collect();
        let sources = vec![samples(&source)];
        let intervals = vec![
            interval(0, 100_000, 7, 0.5),
            interval(20_000, 80_000, 0, 1.0),
            interval(70_000, 120_000, 31, 0.25),
        ];
        let temporary = tempfile::tempdir().unwrap();
        let destination = temporary.path().join("mix.f32");
        write_mix(
            &intervals,
            &sources,
            130_000,
            &destination,
            4,
            &JobControl::default(),
        )
        .unwrap();
        let bytes = std::fs::read(destination).unwrap();
        let actual: Vec<f32> = bytes
            .chunks_exact(4)
            .map(|value| f32::from_le_bytes(value.try_into().unwrap()))
            .collect();
        for frame in 0..130_000 {
            let mut expected = 0.0;
            for interval in &intervals {
                if frame >= interval.first && frame < interval.last {
                    // Independent scalar definition: smoothstep over distance
                    // from either endpoint, with an unchanged middle region.
                    let envelope = |position: u64, length: u64, fade: u64| {
                        if fade == 0 {
                            return 1.0_f32;
                        }
                        let x = (position.min(length - 1 - position) as f32 / fade as f32).min(1.0);
                        x * x * (3.0 - 2.0 * x)
                    };
                    let age = frame - interval.first;
                    let position = (age + interval.offset) % 50;
                    let gain = interval.gain
                        * envelope(age, interval.last - interval.first, 240)
                        * envelope(position, 50, 12);
                    expected += source[position as usize * 2] * gain;
                }
            }
            assert_eq!(actual[frame as usize * 2], expected, "frame {frame}");
            assert_eq!(actual[frame as usize * 2 + 1], expected);
        }
    }
    #[test]
    fn velocity_gain_and_crop_do_not_restart_at_block_boundary() {
        let sources = vec![samples(&[0.1, 0.1, 0.2, 0.2, 0.3, 0.3, 0.4, 0.4])];
        let intervals = vec![interval(3, 20, 2, 64.0 / 127.0)];
        let first = mix_block(
            &BlockTask {
                first: 0,
                frames: 8,
                active: vec![0],
            },
            &intervals,
            &sources,
            &JobControl::default(),
        )
        .unwrap();
        let second = mix_block(
            &BlockTask {
                first: 8,
                frames: 8,
                active: vec![0],
            },
            &intervals,
            &sources,
            &JobControl::default(),
        )
        .unwrap();
        assert_eq!(first[..6], [0.0; 6]);
        assert_eq!(
            first[6], 0.0,
            "a cropped nonzero source starts at zero gain"
        );
        assert!((first[8] - 0.4 * 64.0 / 127.0 * 0.04296875).abs() < 1e-7);
        assert!((second[0] - 0.4 * 64.0 / 127.0 * 0.68359375).abs() < 1e-7);
    }
    #[test]
    fn note_edges_are_smooth_and_end_in_silence_without_extending_the_voice() {
        let sources = vec![samples(&vec![0.5; 96_000])];
        let intervals = vec![interval(400, 20_000, 6000, 1.0)];
        let mixed = mix_block(
            &BlockTask {
                first: 0,
                frames: 21_000,
                active: vec![0],
            },
            &intervals,
            &sources,
            &JobControl::default(),
        )
        .unwrap();
        assert!(mixed[..400 * 2].iter().all(|sample| *sample == 0.0));
        assert_eq!(mixed[400 * 2], 0.0);
        assert_eq!(mixed[(20_000 - 1) * 2], 0.0);
        assert!(mixed[20_000 * 2..].iter().all(|sample| *sample == 0.0));
        assert_eq!(
            mixed[1000 * 2],
            0.5,
            "preserve the original middle amplitude"
        );
        let maximum_jump = mixed
            .chunks_exact(2)
            .map(|frame| frame[0])
            .collect::<Vec<_>>()
            .windows(2)
            .map(|pair| (pair[1] - pair[0]).abs())
            .fold(0.0_f32, f32::max);
        assert!(
            maximum_jump < 0.0032,
            "5 ms fade should reduce a 0.5 hard step: {maximum_jump}"
        );
    }
    #[test]
    fn loop_seams_are_smoothed_without_moving_source_time_or_extending_notes() {
        let period = 4800;
        let source: Vec<f32> = (0..period)
            .flat_map(|index| {
                let value = -0.4 + 0.8 * index as f32 / (period - 1) as f32;
                [value, value]
            })
            .collect();
        let sources = vec![samples(&source)];
        let intervals = vec![interval(130, 20_000, 1200, 1.0)];
        let mixed = mix_block(
            &BlockTask {
                first: 0,
                frames: 21_000,
                active: vec![0],
            },
            &intervals,
            &sources,
            &JobControl::default(),
        )
        .unwrap();
        for seam in [3730_usize, 8530, 13_330, 18_130] {
            assert_eq!(mixed[(seam - 1) * 2], 0.0);
            assert_eq!(mixed[seam * 2], 0.0);
            let maximum_jump = (seam - 245..seam + 245)
                .map(|frame| (mixed[(frame + 1) * 2] - mixed[frame * 2]).abs())
                .fold(0.0_f32, f32::max);
            assert!(
                maximum_jump < 0.004,
                "seam {seam} contains an abrupt step: {maximum_jump}"
            );
        }
        assert_eq!(
            mixed[2530 * 2],
            source[3600 * 2],
            "keep the original sample clock"
        );
        assert!(mixed[20_000 * 2..].iter().all(|sample| *sample == 0.0));
    }
    #[test]
    fn float_mix_keeps_headroom_until_the_final_limiter() {
        let sources = vec![samples(&vec![0.8; 192_000])];
        let intervals = vec![
            interval(0, 48_000, 24_000, 1.0),
            interval(0, 48_000, 24_000, 1.0),
        ];
        let mixed = mix_block(
            &BlockTask {
                first: 1000,
                frames: 8,
                active: vec![0, 1],
            },
            &intervals,
            &sources,
            &JobControl::default(),
        )
        .unwrap();
        assert!(
            mixed.iter().all(|sample| (*sample - 1.6).abs() < 1e-7),
            "overlapping samples must not clip before the final float limiter"
        );
    }
    #[test]
    fn cancellation_stops_before_creating_mix() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("cancel.f32");
        let control = JobControl::default();
        control
            .cancel
            .store(true, std::sync::atomic::Ordering::Relaxed);
        assert!(write_mix(&[], &[], 100, &path, 2, &control).is_err());
        assert!(!path.exists());
    }
    #[test]
    fn cache_fingerprint_tracks_file_version_and_window() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("source.wav");
        std::fs::write(&path, b"a").unwrap();
        let info = MediaInfo {
            path: path.clone(),
            duration: 5.0,
            has_video: false,
            has_audio: true,
        };
        let window = SourceWindow {
            start: 0.0,
            end: 2.0,
        };
        let first = fingerprint(&info, window).unwrap();
        assert_ne!(
            first,
            fingerprint(
                &info,
                SourceWindow {
                    start: 1.0,
                    end: 3.0
                }
            )
            .unwrap()
        );
        std::fs::write(path, b"different").unwrap();
        assert_ne!(first, fingerprint(&info, window).unwrap());
    }
    fn wave_source(path: &Path, amplitude: f64) {
        let mut writer = hound::WavWriter::create(
            path,
            hound::WavSpec {
                channels: 2,
                sample_rate: 48000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )
        .unwrap();
        let frequencies = [220.0, 440.0, 880.0, 1760.0];
        for frame in 0..96000 {
            let frequency = frequencies[frame / 24000];
            let sample = (amplitude
                * 32767.0
                * (std::f64::consts::TAU * frequency * frame as f64 / 48000.0).sin())
            .round() as i16;
            writer.write_sample(sample).unwrap();
            writer.write_sample(sample).unwrap();
        }
        writer.finalize().unwrap();
    }
    fn mono_wave(path: &Path) -> Vec<f64> {
        let mut reader = hound::WavReader::open(path).unwrap();
        assert_eq!(reader.spec().sample_rate, 48000);
        assert_eq!(reader.spec().channels, 2);
        reader
            .samples::<i16>()
            .step_by(2)
            .map(|sample| sample.unwrap() as f64 / 32768.0)
            .collect()
    }
    fn rms(values: &[f64]) -> f64 {
        (values.iter().map(|value| value * value).sum::<f64>() / values.len() as f64).sqrt()
    }
    fn tone(values: &[f64], frequency: f64) -> f64 {
        let (mut sine, mut cosine) = (0.0, 0.0);
        for (index, value) in values.iter().enumerate() {
            let phase = std::f64::consts::TAU * frequency * index as f64 / 48000.0;
            sine += value * phase.sin();
            cosine += value * phase.cos();
        }
        2.0 * (sine * sine + cosine * cosine).sqrt() / values.len() as f64
    }
    #[test]
    fn real_media_window_velocity_cache_and_import_alignment() {
        let ffmpeg =
            crate::media::find_ffmpeg(None).expect("FFmpeg is required for real media tests");
        let temporary = tempfile::tempdir().unwrap();
        let source = temporary.path().join("素材_声部.wav");
        wave_source(&source, 0.1);
        let media = BTreeMap::from([(
            60,
            MediaInfo {
                path: source.clone(),
                duration: 2.0,
                has_video: false,
                has_audio: true,
            },
        )]);
        let data = MidiData {
            notes: vec![
                Note {
                    pitch: 60,
                    start: 0.0,
                    end: 1.0,
                    velocity: 127,
                    channel: 0,
                    track: 0,
                    source_offset: 0.0,
                },
                Note {
                    pitch: 60,
                    start: 1.0,
                    end: 2.0,
                    velocity: 64,
                    channel: 0,
                    track: 0,
                    source_offset: 0.0,
                },
            ],
            duration: 2.0,
            tracks: vec![],
            warnings: vec![],
        };
        let mut options = RenderOptions::default();
        options.materials.insert(
            60,
            MaterialChoice {
                start: 0.5,
                end: Some(1.0),
                random: false,
            },
        );
        options.workers = 2;
        let (planned, windows) = crate::timeline::plan_materials(&data, &options, &media).unwrap();
        let timeline = crate::timeline::build(&planned, 30, options.chord).unwrap();
        let first_work = temporary.path().join("first");
        std::fs::create_dir(&first_work).unwrap();
        let first = render_mix(
            &timeline,
            &media,
            &windows,
            &options,
            &first_work,
            &ffmpeg,
            &JobControl::default(),
        )
        .unwrap();
        let values = mono_wave(&first);
        assert_eq!(values.len(), 96000);
        assert!(tone(&values[4800..43200], 440.0) > 0.095);
        assert!(tone(&values[4800..43200], 880.0) < 0.001);
        let gain = rms(&values[52800..91200]) / rms(&values[4800..43200]);
        assert!((gain - 64.0 / 127.0).abs() < 0.001, "gain {gain}");
        let second_work = temporary.path().join("cache-hit");
        std::fs::create_dir(&second_work).unwrap();
        let second = render_mix(
            &timeline,
            &media,
            &windows,
            &options,
            &second_work,
            &ffmpeg,
            &JobControl::default(),
        )
        .unwrap();
        assert_eq!(mono_wave(&second), values);
        assert!(
            !second_work.join("source-060.f32").exists(),
            "a repeated preview should reuse decoded source PCM"
        );
        wave_source(&source, 0.2);
        let third_work = temporary.path().join("changed-source");
        std::fs::create_dir(&third_work).unwrap();
        let third = render_mix(
            &timeline,
            &media,
            &windows,
            &options,
            &third_work,
            &ffmpeg,
            &JobControl::default(),
        )
        .unwrap();
        let changed = mono_wave(&third);
        let ratio = rms(&changed[4800..43200]) / rms(&values[4800..43200]);
        assert!((ratio - 2.0).abs() < 0.003, "updated source gain {ratio}");
        let imported_timeline = crate::timeline::build(
            &MidiData {
                notes: vec![Note {
                    pitch: 60,
                    start: 0.0,
                    end: 0.5,
                    velocity: 32,
                    channel: 0,
                    track: 0,
                    source_offset: 0.0,
                }],
                duration: 0.5,
                tracks: vec![],
                warnings: vec![],
            },
            30,
            options.chord,
        )
        .unwrap();
        let mut imported = RenderOptions {
            audio_path: Some(source),
            audio_start: 0.75,
            ..RenderOptions::default()
        };
        let imported_work = temporary.path().join("imported");
        std::fs::create_dir(&imported_work).unwrap();
        let output = render_mix(
            &imported_timeline,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &imported,
            &imported_work,
            &ffmpeg,
            &JobControl::default(),
        )
        .unwrap();
        let music = mono_wave(&output);
        assert_eq!(music.len(), 24000);
        assert!(tone(&music[2400..9600], 440.0) > 0.19);
        assert!(tone(&music[14400..21600], 880.0) > 0.19);
        imported.audio_start = 1.9;
        let padded_work = temporary.path().join("padded");
        std::fs::create_dir(&padded_work).unwrap();
        let output = render_mix(
            &imported_timeline,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &imported,
            &padded_work,
            &ffmpeg,
            &JobControl::default(),
        )
        .unwrap();
        let padded = mono_wave(&output);
        assert_eq!(padded.len(), 24000);
        assert!(
            rms(&padded[9600..]) < 0.00001,
            "short imported music should finish with silence"
        );
    }
}
