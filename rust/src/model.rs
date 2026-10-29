use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Note {
    pub pitch: u8,
    pub start: f64,
    pub end: f64,
    pub velocity: u8,
    pub channel: u8,
    pub track: usize,
    #[serde(default)]
    pub source_offset: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct TrackInfo {
    pub index: usize,
    pub name: String,
    pub channels: Vec<u8>,
    pub note_count: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct MidiData {
    pub notes: Vec<Note>,
    pub tracks: Vec<TrackInfo>,
    pub duration: f64,
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Selection {
    pub tracks: Option<Vec<usize>>,
    pub channels: Option<Vec<u8>>,
    pub start: f64,
    pub end: Option<f64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct MaterialChoice {
    pub start: f64,
    pub end: Option<f64>,
    pub random: bool,
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum VisualMode {
    #[default]
    Labels,
    Clips,
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ChordPolicy {
    #[default]
    All,
    Highest,
    Lowest,
    Latest,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RenderOptions {
    pub video_dir: PathBuf,
    pub output: PathBuf,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub visuals: VisualMode,
    pub audio_path: Option<PathBuf>,
    pub audio_start: f64,
    pub velocity: bool,
    pub materials: BTreeMap<u8, MaterialChoice>,
    pub seed: u64,
    pub ffmpeg: Option<PathBuf>,
    pub workers: usize,
    pub overwrite: bool,
    pub chord: ChordPolicy,
}

impl Default for RenderOptions {
    fn default() -> Self {
        Self {
            video_dir: "video".into(),
            output: "output.mp4".into(),
            width: 1280,
            height: 720,
            fps: 30,
            visuals: VisualMode::Labels,
            audio_path: None,
            audio_start: 0.0,
            velocity: true,
            materials: BTreeMap::new(),
            seed: 0,
            ffmpeg: None,
            workers: 0,
            overwrite: false,
            chord: ChordPolicy::All,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MediaInfo {
    pub path: PathBuf,
    pub duration: f64,
    pub has_video: bool,
    pub has_audio: bool,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct SourceWindow {
    pub start: f64,
    pub end: f64,
}
impl SourceWindow {
    pub fn duration(&self) -> f64 {
        self.end - self.start
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Voice {
    pub note_id: usize,
    pub pitch: u8,
    pub source_offset: f64,
    pub velocity: u8,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Segment {
    pub first: u64,
    pub last: u64,
    pub voices: Vec<Voice>,
}
impl Segment {
    pub fn frames(&self) -> u64 {
        self.last - self.first
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VoiceSpan {
    pub note_id: usize,
    pub pitch: u8,
    pub first_frame: u64,
    pub last_frame: u64,
    pub source_offset: f64,
    pub velocity: u8,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Timeline {
    pub total_frames: u64,
    pub fps: u32,
    pub segments: Vec<Segment>,
    pub voices: Vec<VoiceSpan>,
}
impl Timeline {
    pub fn seconds(&self) -> f64 {
        self.total_frames as f64 / self.fps as f64
    }
}

#[derive(Clone, Debug)]
pub struct Progress {
    pub fraction: f64,
    pub message: String,
}

#[derive(Clone)]
pub struct JobControl {
    pub cancel: Arc<AtomicBool>,
    pub progress: Arc<dyn Fn(Progress) + Send + Sync>,
}
impl Default for JobControl {
    fn default() -> Self {
        Self {
            cancel: Arc::new(AtomicBool::new(false)),
            progress: Arc::new(|_| {}),
        }
    }
}
impl JobControl {
    pub fn check(&self) -> Result<()> {
        if self.cancel.load(Ordering::Relaxed) {
            bail!("已取消，原输出文件未修改。")
        }
        Ok(())
    }
    pub fn report(&self, fraction: f64, message: impl Into<String>) {
        (self.progress)(Progress {
            fraction: fraction.clamp(0.0, 1.0),
            message: message.into(),
        });
    }
    pub fn scoped(&self, start: f64, end: f64, label: &str) -> Self {
        let parent = self.clone();
        let label = label.to_owned();
        Self {
            cancel: self.cancel.clone(),
            progress: Arc::new(move |event: Progress| {
                parent.report(
                    start + (end - start) * event.fraction,
                    if label.is_empty() {
                        event.message
                    } else {
                        format!("{label}：{}", event.message)
                    },
                );
            }),
        }
    }
}

pub fn note_name(pitch: u8) -> String {
    const NAMES: [&str; 12] = [
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    ];
    format!("{}{}", NAMES[pitch as usize % 12], pitch as i32 / 12 - 1)
}

pub fn frequency(pitch: u8) -> f64 {
    440.0 * 2.0_f64.powf((pitch as f64 - 69.0) / 12.0)
}
