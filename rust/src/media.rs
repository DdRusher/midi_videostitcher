use crate::model::{note_name, JobControl, MediaInfo, MidiData, RenderOptions};
use crate::process;
use anyhow::{bail, Context, Result};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::UNIX_EPOCH;

const VIDEO: &[&str] = &["mp4", "mov", "mkv", "webm", "avi", "m4v"];
const AUDIO: &[&str] = &["wav", "flac", "mp3", "m4a", "ogg", "aac", "wma"];
type MediaKey = (PathBuf, u64, u128);
static INFO_CACHE: OnceLock<Mutex<BTreeMap<MediaKey, MediaInfo>>> = OnceLock::new();

fn search_path(name: &str) -> Option<PathBuf> {
    let executable = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    };
    if let Ok(current) = std::env::current_exe() {
        if let Some(parent) = current.parent() {
            let candidate = parent.join(&executable);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    for directory in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        let candidate = directory.join(&executable);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

pub fn find_ffmpeg(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        if path.is_file() {
            return Ok(path.canonicalize()?);
        }
        if let Some(name) = path.to_str() {
            let name = name.strip_suffix(".exe").unwrap_or(name);
            if let Some(found) = search_path(name) {
                return Ok(found.canonicalize()?);
            }
        }
        bail!("找不到指定的 FFmpeg：{}", path.display());
    }
    search_path("ffmpeg")
        .context("找不到 FFmpeg。请把 ffmpeg 放到程序同目录，或在窗口中指定路径。")?
        .canonicalize()
        .map_err(Into::into)
}

fn find_ffprobe(ffmpeg: &Path) -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "ffprobe.exe"
    } else {
        "ffprobe"
    };
    ffmpeg
        .parent()
        .map(|parent| parent.join(name))
        .filter(|path| path.is_file())
        .or_else(|| search_path("ffprobe"))
}

fn key(path: &Path) -> Result<MediaKey> {
    let metadata = path.metadata()?;
    Ok((
        path.canonicalize()?,
        metadata.len(),
        metadata
            .modified()?
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos(),
    ))
}

pub fn fingerprint(path: &Path) -> Result<String> {
    let key = key(path)?;
    Ok(
        blake3::hash(format!("{}|{}|{}", key.0.to_string_lossy(), key.1, key.2).as_bytes())
            .to_hex()
            .to_string(),
    )
}

pub fn find_assets(
    data: &MidiData,
    directory: &Path,
    allow_audio: bool,
) -> Result<BTreeMap<u8, PathBuf>> {
    if !directory.is_dir() {
        bail!("素材文件夹不存在：{}", directory.display());
    }
    let mut names: BTreeMap<String, Vec<PathBuf>> = BTreeMap::new();
    for entry in fs::read_dir(directory)? {
        let path = entry?.path();
        if !path.is_file() {
            continue;
        }
        let extension = path
            .extension()
            .and_then(OsStr::to_str)
            .unwrap_or("")
            .to_ascii_lowercase();
        if !VIDEO.contains(&extension.as_str())
            && !(allow_audio && AUDIO.contains(&extension.as_str()))
        {
            continue;
        }
        if let Some(stem) = path.file_stem().and_then(OsStr::to_str) {
            names
                .entry(stem.to_ascii_uppercase())
                .or_default()
                .push(path.canonicalize()?);
        }
    }
    let pitches: BTreeSet<_> = data.notes.iter().map(|note| note.pitch).collect();
    let mut output = BTreeMap::new();
    let mut missing = Vec::new();
    const FLATS: [&str; 12] = [
        "C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B",
    ];
    for pitch in pitches {
        if pitch > 127 {
            bail!("MIDI 音高超出范围：{pitch}");
        }
        let aliases = [
            note_name(pitch).to_ascii_uppercase(),
            format!("{}{}", FLATS[pitch as usize % 12], pitch as i32 / 12 - 1).to_ascii_uppercase(),
            pitch.to_string(),
        ];
        let matches: BTreeSet<_> = aliases
            .iter()
            .filter_map(|alias| names.get(alias))
            .flatten()
            .cloned()
            .collect();
        if matches.len() > 1 {
            bail!(
                "{} 有多个素材候选，请只保留一份：{}",
                note_name(pitch),
                matches
                    .iter()
                    .map(|p| p.file_name().unwrap_or_default().to_string_lossy())
                    .collect::<Vec<_>>()
                    .join("、")
            );
        }
        match matches.into_iter().next() {
            Some(path) => {
                output.insert(pitch, path);
            }
            None => missing.push(note_name(pitch)),
        }
    }
    if !missing.is_empty() {
        bail!(
            "缺少以下音符的素材：{}。例如 C4.mp4 或 60.wav。",
            missing.join("、")
        );
    }
    Ok(output)
}

fn clock(text: &str) -> Option<f64> {
    let value = text
        .trim()
        .split(|character: char| character == ',' || character.is_whitespace())
        .next()?;
    let fields: Vec<_> = value.split(':').collect();
    if fields.len() != 3 {
        return None;
    }
    let duration = fields[0].parse::<f64>().ok()? * 3600.0
        + fields[1].parse::<f64>().ok()? * 60.0
        + fields[2].parse::<f64>().ok()?;
    (duration.is_finite() && duration > 0.0).then_some(duration)
}

fn mp4_duration(path: &Path) -> Option<f64> {
    fn boxes(file: &mut File, start: u64, end: u64, depth: u8) -> std::io::Result<Option<f64>> {
        if depth > 4 {
            return Ok(None);
        }
        let mut position = start;
        while position.checked_add(8).map_or(false, |value| value <= end) {
            file.seek(SeekFrom::Start(position))?;
            let mut header = [0; 8];
            file.read_exact(&mut header)?;
            let mut length = u32::from_be_bytes(header[..4].try_into().unwrap()) as u64;
            let mut header_size = 8;
            if length == 1 {
                let mut wide = [0; 8];
                file.read_exact(&mut wide)?;
                length = u64::from_be_bytes(wide);
                header_size = 16;
            }
            if length == 0 {
                length = end - position;
            }
            if length < header_size
                || position
                    .checked_add(length)
                    .map_or(true, |value| value > end)
            {
                break;
            }
            if &header[4..] == b"moov" {
                if let Some(value) =
                    boxes(file, position + header_size, position + length, depth + 1)?
                {
                    return Ok(Some(value));
                }
            } else if &header[4..] == b"mvhd" {
                let mut value = vec![0; (length - header_size).min(32) as usize];
                file.read_exact(&mut value)?;
                let (rate, duration) = if value.first() == Some(&0) && value.len() >= 20 {
                    (
                        u32::from_be_bytes(value[12..16].try_into().unwrap()),
                        u32::from_be_bytes(value[16..20].try_into().unwrap()) as u64,
                    )
                } else if value.first() == Some(&1) && value.len() >= 32 {
                    (
                        u32::from_be_bytes(value[20..24].try_into().unwrap()),
                        u64::from_be_bytes(value[24..32].try_into().unwrap()),
                    )
                } else {
                    return Ok(None);
                };
                if rate > 0 && duration > 0 {
                    return Ok(Some(duration as f64 / rate as f64));
                }
            }
            position += length;
        }
        Ok(None)
    }
    let mut file = File::open(path).ok()?;
    let size = file.metadata().ok()?.len();
    boxes(&mut file, 0, size, 0).ok().flatten()
}

fn parse_info(path: &Path, text: &str, ffmpeg: &Path, control: &JobControl) -> Result<MediaInfo> {
    let mut duration = text
        .lines()
        .find_map(|line| {
            line.split_once("Duration:")
                .and_then(|(_, value)| clock(value))
        })
        .unwrap_or(0.0);
    let has_video = text
        .lines()
        .any(|line| line.contains("Stream #") && line.contains(": Video:"));
    let has_audio = text
        .lines()
        .any(|line| line.contains("Stream #") && line.contains(": Audio:"));
    let tags = text
        .lines()
        .filter(|line| line.contains("DURATION"))
        .filter_map(|line| line.split_once(':').and_then(|(_, value)| clock(value)))
        .fold(0.0_f64, f64::max);
    let extension = path
        .extension()
        .and_then(OsStr::to_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let precise = if tags > 0.0 {
        Some(tags)
    } else if extension == "wav" {
        hound::WavReader::open(path)
            .ok()
            .map(|reader| reader.duration() as f64 / reader.spec().sample_rate as f64)
    } else if ["mp4", "m4a", "mov", "m4v"].contains(&extension.as_str()) {
        mp4_duration(path)
    } else {
        None
    };
    if let Some(value) = precise {
        duration = value;
    } else if let Some(ffprobe) = find_ffprobe(ffmpeg) {
        let mut arguments = process::args(&[
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
        ]);
        arguments.push(path.as_os_str().into());
        let output = process::run(&ffprobe, &arguments, None, control, 0.0)?;
        if output.code == 0 {
            if let Ok(value) = serde_json::from_slice::<serde_json::Value>(&output.stdout) {
                if let Some(value) = value["format"]["duration"]
                    .as_str()
                    .and_then(|value| value.parse::<f64>().ok())
                {
                    duration = value;
                }
            }
        }
    }
    if !duration.is_finite() || duration <= 0.0 || (!has_video && !has_audio) {
        bail!("无法读取素材时长或音视频轨道：{}", path.display());
    }
    Ok(MediaInfo {
        path: path.to_path_buf(),
        duration,
        has_video,
        has_audio,
    })
}

pub fn probe_many(
    ffmpeg: &Path,
    paths: &[PathBuf],
    control: &JobControl,
) -> Result<BTreeMap<PathBuf, MediaInfo>> {
    let cache = INFO_CACHE.get_or_init(Mutex::default);
    let mut output = BTreeMap::new();
    let mut pending = Vec::new();
    for path in paths.iter().cloned().collect::<BTreeSet<_>>() {
        control.check()?;
        let fingerprint = key(&path)?;
        if let Some(info) = cache.lock().unwrap().get(&fingerprint).cloned() {
            output.insert(path, info);
        } else {
            pending.push((path, fingerprint));
        }
    }
    for batch in pending.chunks(16) {
        let mut arguments = process::args(&["-hide_banner", "-nostdin"]);
        for (path, _) in batch {
            arguments.push("-i".into());
            arguments.push(path.as_os_str().into());
        }
        let result = process::run(ffmpeg, &arguments, None, control, 0.0)?;
        let text = String::from_utf8_lossy(&result.stderr);
        let mut sections: BTreeMap<usize, String> = BTreeMap::new();
        let mut current = None;
        for line in text.lines() {
            if let Some(value) = line.strip_prefix("Input #") {
                current = value
                    .split(',')
                    .next()
                    .and_then(|value| value.parse::<usize>().ok());
            }
            if let Some(index) = current {
                let entry = sections.entry(index).or_default();
                entry.push_str(line);
                entry.push('\n');
            }
        }
        for (index, (path, fingerprint)) in batch.iter().enumerate() {
            let info = parse_info(
                path,
                sections.get(&index).map(String::as_str).unwrap_or(""),
                ffmpeg,
                control,
            )?;
            let mut cache = cache.lock().unwrap();
            if cache.len() > 2048 {
                cache.clear();
            }
            cache.insert(fingerprint.clone(), info.clone());
            output.insert(path.clone(), info);
        }
    }
    Ok(output)
}

pub fn inspect(
    data: &MidiData,
    options: &RenderOptions,
    control: &JobControl,
) -> Result<BTreeMap<u8, MediaInfo>> {
    let files = find_assets(data, &options.video_dir, true)?;
    let ffmpeg = find_ffmpeg(options.ffmpeg.as_deref())?;
    let infos = probe_many(
        &ffmpeg,
        &files.values().cloned().collect::<Vec<_>>(),
        control,
    )?;
    Ok(files
        .into_iter()
        .map(|(pitch, path)| (pitch, infos[&path].clone()))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Note;
    fn data(pitch: u8) -> MidiData {
        MidiData {
            notes: vec![Note {
                pitch,
                start: 0.,
                end: 1.,
                velocity: 127,
                channel: 0,
                track: 0,
                source_offset: 0.,
            }],
            tracks: vec![],
            duration: 1.,
            warnings: vec![],
        }
    }
    #[test]
    fn aliases_and_ambiguity_are_checked() {
        let temporary = tempfile::tempdir().unwrap();
        fs::write(temporary.path().join("Db4.wav"), []).unwrap();
        assert_eq!(
            find_assets(&data(61), temporary.path(), true)
                .unwrap()
                .len(),
            1
        );
        fs::write(temporary.path().join("61.mp4"), []).unwrap();
        assert!(find_assets(&data(61), temporary.path(), true)
            .unwrap_err()
            .to_string()
            .contains("多个"));
    }
    #[test]
    fn precise_wav_duration_and_missing_files() {
        let temporary = tempfile::tempdir().unwrap();
        assert!(find_assets(&data(60), temporary.path(), true)
            .unwrap_err()
            .to_string()
            .contains("C4"));
        assert_eq!(clock("00:01:02.125, bitrate: 128"), Some(62.125));
    }
}
