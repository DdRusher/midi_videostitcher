//! Native Chinese UI. Heavy work always runs in a cancellable background thread.
use crate::{engine, midi, model::*};
use anyhow::{anyhow, bail, Context, Result};
use eframe::egui::{self, Color32, FontFamily, RichText, Sense};
use std::{
    collections::{BTreeMap, BTreeSet},
    hash::{Hash, Hasher},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
const BLUE: Color32 = Color32::from_rgb(87, 82, 212);
const MUTED: Color32 = Color32::from_rgb(110, 120, 140);
const INK: Color32 = Color32::from_rgb(30, 42, 62);
const BACKGROUND: Color32 = Color32::from_rgb(246, 248, 252);
const BORDER: Color32 = Color32::from_rgb(225, 230, 239);
const TINT: Color32 = Color32::from_rgb(239, 237, 255);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Workspace {
    Input,
    Materials,
    Output,
}

fn card(ui: &mut egui::Ui, title: &str, description: &str, body: impl FnOnce(&mut egui::Ui)) {
    egui::Frame::new()
        .fill(Color32::WHITE)
        .stroke(egui::Stroke::new(1_f32, BORDER))
        .corner_radius(12)
        .inner_margin(18)
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.label(RichText::new(title).size(18.).strong().color(INK));
            if !description.is_empty() {
                ui.label(RichText::new(description).size(13.).color(MUTED));
            }
            ui.add_space(12.);
            body(ui);
        });
    ui.add_space(14.);
}

fn primary_button(text: &str) -> egui::Button<'_> {
    egui::Button::new(RichText::new(text).color(Color32::WHITE).strong())
        .fill(BLUE)
        .stroke(egui::Stroke::NONE)
        .min_size(egui::vec2(122., 38.))
}

pub fn launch() -> eframe::Result<()> {
    launch_with_midi(None)
}
pub fn launch_with_midi(initial: Option<PathBuf>) -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1180., 860.])
            .with_min_inner_size([880., 680.]),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "MIDI 视频拼接器 · Rust",
        options,
        Box::new(move |cc| Ok(Box::new(StitcherApp::new(cc, initial)))),
    )
}
fn install_fonts(ctx: &egui::Context) {
    let mut paths = vec![];
    if let Some(root) = std::env::var_os("WINDIR") {
        paths.push(PathBuf::from(root).join("Fonts/msyh.ttc"));
    }
    paths.extend(
        [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
        .map(PathBuf::from),
    );
    for path in paths {
        if let Ok(bytes) = std::fs::read(path) {
            let mut fonts = egui::FontDefinitions::default();
            fonts.font_data.insert(
                "chinese".into(),
                Arc::new(egui::FontData::from_owned(bytes)),
            );
            fonts
                .families
                .entry(FontFamily::Proportional)
                .or_default()
                .insert(0, "chinese".into());
            fonts
                .families
                .entry(FontFamily::Monospace)
                .or_default()
                .push("chinese".into());
            ctx.set_fonts(fonts);
            break;
        }
    }
    let mut style = (*ctx.style()).clone();
    style.spacing.item_spacing = egui::vec2(9., 8.);
    style.spacing.button_padding = egui::vec2(13., 8.);
    style.spacing.interact_size.y = 34.;
    style.spacing.combo_width = 160.;
    style
        .text_styles
        .insert(egui::TextStyle::Body, egui::FontId::proportional(15.));
    style
        .text_styles
        .insert(egui::TextStyle::Button, egui::FontId::proportional(15.));
    style
        .text_styles
        .insert(egui::TextStyle::Heading, egui::FontId::proportional(22.));
    let mut visuals = egui::Visuals::light();
    visuals.override_text_color = Some(INK);
    visuals.panel_fill = BACKGROUND;
    visuals.window_fill = Color32::WHITE;
    visuals.extreme_bg_color = Color32::WHITE;
    visuals.faint_bg_color = BACKGROUND;
    visuals.selection.bg_fill = BLUE;
    visuals.selection.stroke = egui::Stroke::new(1_f32, Color32::WHITE);
    visuals.widgets.noninteractive.bg_stroke = egui::Stroke::new(1_f32, BORDER);
    visuals.widgets.noninteractive.fg_stroke = egui::Stroke::new(1_f32, MUTED);
    visuals.widgets.inactive.bg_fill = Color32::WHITE;
    visuals.widgets.inactive.weak_bg_fill = Color32::from_rgb(249, 250, 253);
    visuals.widgets.inactive.bg_stroke = egui::Stroke::new(1_f32, BORDER);
    visuals.widgets.inactive.fg_stroke = egui::Stroke::new(1_f32, INK);
    visuals.widgets.inactive.corner_radius = 7.into();
    visuals.widgets.hovered.bg_fill = TINT;
    visuals.widgets.hovered.weak_bg_fill = TINT;
    visuals.widgets.hovered.bg_stroke = egui::Stroke::new(1_f32, BLUE.gamma_multiply(0.5));
    visuals.widgets.hovered.fg_stroke = egui::Stroke::new(1_f32, BLUE);
    visuals.widgets.hovered.corner_radius = 7.into();
    visuals.widgets.active.bg_fill = TINT;
    visuals.widgets.active.weak_bg_fill = TINT;
    visuals.widgets.active.fg_stroke = egui::Stroke::new(1_f32, BLUE);
    visuals.widgets.active.corner_radius = 7.into();
    visuals.hyperlink_color = BLUE;
    visuals.error_fg_color = Color32::from_rgb(187, 55, 71);
    style.visuals = visuals;
    ctx.set_style(style);
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum JobKind {
    Read,
    Inspect,
    Preview,
    Audio,
    Video,
}
#[derive(Clone, Copy, Debug)]
enum Boundary {
    Start,
    End,
    New(f64),
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct FileStamp {
    length: u64,
    modified: u128,
}
fn file_stamp(path: &Path) -> Result<FileStamp> {
    let m = path
        .metadata()
        .with_context(|| format!("无法读取：{}", path.display()))?;
    Ok(FileStamp {
        length: m.len(),
        modified: m
            .modified()?
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos(),
    })
}
fn absolute(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| {
        if path.is_absolute() {
            path.into()
        } else {
            std::env::current_dir().unwrap_or_default().join(path)
        }
    })
}
fn time_text(seconds: f64) -> String {
    let n = (seconds.max(0.) * 1000.).floor() as u64;
    format!("{}:{:02}.{:03}", n / 60000, n / 1000 % 60, n % 1000)
}
fn preview_key(data: &MidiData, options: &RenderOptions) -> Result<u64> {
    let mut options = options.clone();
    options.output = PathBuf::new();
    options.overwrite = false;
    let mut h = std::collections::hash_map::DefaultHasher::new();
    serde_json::to_vec(&(data, &options))?.hash(&mut h);
    let paths = if let Some(path) = options.audio_path {
        vec![path]
    } else {
        let mut paths = std::fs::read_dir(options.video_dir)
            .context("无法读取素材目录。")?
            .filter_map(|entry| entry.ok().map(|entry| entry.path()))
            .filter(|path| path.is_file())
            .collect::<Vec<_>>();
        paths.sort();
        paths
    };
    for path in paths {
        absolute(&path).hash(&mut h);
        let s = file_stamp(&path)?;
        s.length.hash(&mut h);
        s.modified.hash(&mut h);
    }
    Ok(h.finish())
}
fn validate_material(choice: &MaterialChoice, pitch: u8, info: &MediaInfo) -> Result<()> {
    let end = choice.end.unwrap_or(info.duration);
    if !choice.start.is_finite()
        || !end.is_finite()
        || choice.start < 0.
        || choice.start >= end
        || end > info.duration + 0.000001
    {
        bail!(
            "{} 素材范围应满足 0 ≤ 开始 < 结束 ≤ {:.3} 秒。",
            note_name(pitch),
            info.duration
        );
    }
    Ok(())
}
fn confirm_replace(path: &Path) -> bool {
    !path.exists()
        || rfd::MessageDialog::new()
            .set_title("覆盖已有文件")
            .set_description(format!("文件已存在：\n{}\n\n是否覆盖？", path.display()))
            .set_level(rfd::MessageLevel::Warning)
            .set_buttons(rfd::MessageButtons::YesNo)
            .show()
            == rfd::MessageDialogResult::Yes
}
fn open_path(path: &Path) -> Result<()> {
    if !path.exists() {
        bail!("结果文件或目录已不存在，请重新导出。");
    }
    #[cfg(target_os = "windows")]
    let path = shell_path(path);
    #[cfg(target_os = "windows")]
    let mut cmd = Command::new("explorer.exe");
    #[cfg(target_os = "macos")]
    let mut cmd = Command::new("open");
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    let mut cmd = Command::new("xdg-open");
    cmd.arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("无法打开结果或目录。")?;
    Ok(())
}
#[cfg(target_os = "windows")]
fn shell_path(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(unc) = text.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{unc}"))
    } else if let Some(normal) = text.strip_prefix(r"\\?\") {
        PathBuf::from(normal)
    } else {
        path.to_owned()
    }
}
#[derive(Default)]
struct PreviewPlayer {
    active: bool,
    deadline: Option<Instant>,
    process: Option<Child>,
}
impl PreviewPlayer {
    fn play(&mut self, path: &Path) -> Result<()> {
        self.stop();
        let wav = hound::WavReader::open(path).context("无法读取试听 WAV。")?;
        let seconds = wav.duration() as f64 / wav.spec().sample_rate as f64;
        if seconds <= 0. {
            bail!("试听文件没有有效时长。");
        }
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::ffi::OsStrExt;
            let name: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
            if unsafe { native_audio::PlaySoundW(name.as_ptr(), std::ptr::null_mut(), 0x0002_0003) }
                == 0
            {
                bail!("无法播放试听，请检查系统声音输出设备。");
            }
        }
        #[cfg(not(target_os = "windows"))]
        {
            let candidates: &[(&str, &[&str])] = if cfg!(target_os = "macos") {
                &[
                    ("afplay", &[]),
                    ("ffplay", &["-nodisp", "-autoexit", "-loglevel", "error"]),
                ]
            } else {
                &[
                    ("ffplay", &["-nodisp", "-autoexit", "-loglevel", "error"]),
                    ("aplay", &["-q"]),
                ]
            };
            let mut child = None;
            for (name, args) in candidates {
                if let Ok(p) = Command::new(name)
                    .args(*args)
                    .arg(path)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()
                {
                    child = Some(p);
                    break;
                }
            }
            self.process =
                Some(child.ok_or_else(|| {
                    anyhow!("找不到试听播放器，请安装 FFplay 或打开导出的 WAV。")
                })?);
        }
        self.active = true;
        self.deadline = Some(Instant::now() + Duration::from_secs_f64(seconds));
        Ok(())
    }
    fn stop(&mut self) {
        #[cfg(target_os = "windows")]
        if self.active {
            unsafe {
                native_audio::PlaySoundW(std::ptr::null(), std::ptr::null_mut(), 0);
            }
        }
        if let Some(mut process) = self.process.take() {
            let _ = process.kill();
            let _ = process.wait();
        }
        self.active = false;
        self.deadline = None;
    }
    fn poll(&mut self) -> Result<bool> {
        if let Some(process) = self.process.as_mut() {
            if let Some(status) = process.try_wait()? {
                self.process = None;
                self.active = false;
                self.deadline = None;
                if !status.success() {
                    bail!("试听播放失败，请检查声音输出设备。");
                }
            }
        }
        if self.active
            && self
                .deadline
                .is_some_and(|deadline| Instant::now() >= deadline)
        {
            self.stop();
        }
        Ok(self.active)
    }
}
impl Drop for PreviewPlayer {
    fn drop(&mut self) {
        self.stop();
    }
}
#[cfg(target_os = "windows")]
mod native_audio {
    #[link(name = "winmm")]
    extern "system" {
        pub fn PlaySoundW(sound: *const u16, module: *mut std::ffi::c_void, flags: u32) -> i32;
    }
}
enum JobOutput {
    Read {
        path: PathBuf,
        stamp: FileStamp,
        data: MidiData,
    },
    Inspect(BTreeMap<u8, MediaInfo>),
    Preview {
        path: PathBuf,
        key: u64,
    },
    Export(PathBuf),
}
enum Event {
    Progress(u64, Progress),
    Done(u64, JobKind, std::result::Result<JobOutput, String>),
}
struct ActiveJob {
    id: u64,
    cancel: Arc<AtomicBool>,
    thread: JoinHandle<()>,
}
pub struct StitcherApp {
    workspace: Workspace,
    midi_path: String,
    video_dir: String,
    output_path: String,
    imported_audio: bool,
    audio_path: String,
    options: RenderOptions,
    data: Option<MidiData>,
    loaded_path: Option<PathBuf>,
    loaded_stamp: Option<FileStamp>,
    selected_tracks: BTreeSet<usize>,
    channel: Option<u8>,
    start: String,
    end: String,
    infos: BTreeMap<u8, MediaInfo>,
    selected_material: Option<u8>,
    material_start: String,
    material_end: String,
    material_random: bool,
    material_dirty: bool,
    drag: Option<Boundary>,
    tx: mpsc::Sender<Event>,
    rx: mpsc::Receiver<Event>,
    job: Option<ActiveJob>,
    next_job: u64,
    fraction: f64,
    status: String,
    error: Option<String>,
    logs: Vec<String>,
    last_output: Option<PathBuf>,
    player: PreviewPlayer,
    preview_temp: Option<tempfile::TempDir>,
    preview_cache: Option<(u64, PathBuf)>,
    closing: bool,
    pending_read: bool,
}
impl StitcherApp {
    fn new(cc: &eframe::CreationContext<'_>, initial: Option<PathBuf>) -> Self {
        install_fonts(&cc.egui_ctx);
        Self::new_state(initial)
    }
    fn new_state(initial: Option<PathBuf>) -> Self {
        let (tx, rx) = mpsc::channel();
        let output = initial.as_ref().map(|p| p.with_extension("stitched.mp4"));
        Self {
            workspace: Workspace::Input,
            midi_path: initial
                .as_ref()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default(),
            video_dir: String::new(),
            output_path: output
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default(),
            imported_audio: false,
            audio_path: String::new(),
            options: RenderOptions::default(),
            data: None,
            loaded_path: None,
            loaded_stamp: None,
            selected_tracks: BTreeSet::new(),
            channel: None,
            start: "0".into(),
            end: String::new(),
            infos: BTreeMap::new(),
            selected_material: None,
            material_start: "0".into(),
            material_end: String::new(),
            material_random: false,
            material_dirty: false,
            drag: None,
            tx,
            rx,
            job: None,
            next_job: 0,
            fraction: 0.,
            status: "选择 MIDI，设置素材或导入音乐后即可试听与导出。".into(),
            error: None,
            logs: vec![],
            last_output: None,
            player: PreviewPlayer::default(),
            preview_temp: tempfile::Builder::new()
                .prefix("midi-rust-preview-")
                .tempdir()
                .ok(),
            preview_cache: None,
            closing: false,
            pending_read: initial.is_some(),
        }
    }
    fn busy(&self) -> bool {
        self.job.is_some()
    }
    fn log(&mut self, s: impl Into<String>) {
        let s = s.into();
        if self.logs.last() != Some(&s) {
            self.logs.push(s);
            if self.logs.len() > 200 {
                self.logs.drain(..50);
            }
        }
    }
    fn fail(&mut self, error: impl std::fmt::Display) {
        self.error = Some(error.to_string());
        self.status = "操作未完成，请查看提示。".into();
        self.log(error.to_string());
    }
    fn invalidate_preview(&mut self) {
        self.player.stop();
        if let Some((_, path)) = self.preview_cache.take() {
            let _ = std::fs::remove_file(path);
        }
    }
    fn set_midi_path(&mut self, path: String) {
        if self.midi_path == path {
            return;
        }
        self.midi_path = path;
        self.data = None;
        self.loaded_path = None;
        self.loaded_stamp = None;
        self.selected_tracks.clear();
        self.start = "0".into();
        self.end.clear();
        self.infos.clear();
        self.selected_material = None;
        self.material_dirty = false;
        self.invalidate_preview();
    }
    fn set_video_dir(&mut self, path: String) {
        if self.video_dir == path {
            return;
        }
        self.video_dir = path;
        self.infos.clear();
        self.options.materials.clear();
        self.selected_material = None;
        self.material_dirty = false;
        self.invalidate_preview();
    }
    fn selected_data(&self) -> Result<MidiData> {
        let data = self
            .data
            .as_ref()
            .ok_or_else(|| anyhow!("请先读取 MIDI。"))?;
        let path = self
            .loaded_path
            .as_ref()
            .ok_or_else(|| anyhow!("请重新读取 MIDI。"))?;
        if Some(file_stamp(path)?) != self.loaded_stamp {
            bail!("MIDI 文件已更改，请重新读取。");
        }
        let start = midi::parse_time(self.start.trim())?;
        let end = if self.end.trim().is_empty() {
            None
        } else {
            Some(midi::parse_time(self.end.trim())?)
        };
        midi::select(
            data,
            &Selection {
                tracks: Some(self.selected_tracks.iter().copied().collect()),
                channels: self.channel.map(|v| vec![v]),
                start,
                end,
            },
        )
    }
    fn needed_materials(&self) -> BTreeMap<u8, f64> {
        let mut pitches = BTreeMap::<u8, f64>::new();
        if let Ok(data) = self.selected_data() {
            for n in data.notes {
                let duration = n.end - n.start + n.source_offset;
                pitches
                    .entry(n.pitch)
                    .and_modify(|d| *d = d.max(duration))
                    .or_insert(duration);
            }
        }
        pitches
    }
    fn apply_material(&mut self, pitches: &[u8]) -> Result<()> {
        if pitches.is_empty() {
            bail!("请先选择素材。");
        }
        let start = midi::parse_time(if self.material_start.trim().is_empty() {
            "0"
        } else {
            self.material_start.trim()
        })?;
        let end = if self.material_end.trim().is_empty() {
            None
        } else {
            Some(midi::parse_time(self.material_end.trim())?)
        };
        let choice = MaterialChoice {
            start,
            end,
            random: self.material_random,
        };
        for pitch in pitches {
            validate_material(
                &choice,
                *pitch,
                self.infos
                    .get(pitch)
                    .ok_or_else(|| anyhow!("请先检查素材，读取素材时长。"))?,
            )?;
        }
        for pitch in pitches {
            self.options.materials.insert(*pitch, choice.clone());
        }
        self.material_dirty = false;
        self.invalidate_preview();
        Ok(())
    }
    fn choose_material(&mut self, pitch: u8) -> Result<()> {
        if self.selected_material == Some(pitch) {
            return Ok(());
        }
        if self.material_dirty {
            if let Some(old) = self.selected_material {
                self.apply_material(&[old])?;
            }
        }
        self.selected_material = Some(pitch);
        let choice = self
            .options
            .materials
            .get(&pitch)
            .cloned()
            .unwrap_or_default();
        self.material_start = choice.start.to_string();
        self.material_end = choice.end.map(|v| v.to_string()).unwrap_or_default();
        self.material_random = choice.random;
        self.material_dirty = false;
        Ok(())
    }
    fn make_options(&mut self, output: PathBuf, audio_only: bool) -> Result<RenderOptions> {
        if self.material_dirty && !self.imported_audio {
            if let Some(pitch) = self.selected_material {
                self.apply_material(&[pitch])?;
            }
        }
        let pitches: BTreeSet<_> = self
            .selected_data()?
            .notes
            .iter()
            .map(|n| n.pitch)
            .collect();
        let mut options = self.options.clone();
        options.output = absolute(&output);
        options.materials.retain(|pitch, _| pitches.contains(pitch));
        if self.video_dir.trim().is_empty()
            && !(self.imported_audio && (audio_only || options.visuals == VisualMode::Labels))
        {
            bail!("请选择素材目录，或导入音乐并使用音名画面。");
        }
        options.video_dir = absolute(Path::new(if self.video_dir.trim().is_empty() {
            "."
        } else {
            self.video_dir.trim()
        }));
        options.audio_path = if self.imported_audio {
            if self.audio_path.trim().is_empty() {
                bail!("请选择要导入的音频。");
            }
            let path = absolute(Path::new(self.audio_path.trim()));
            if !path.is_file() {
                bail!("导入音频不存在：{}", path.display());
            }
            Some(path)
        } else {
            None
        };
        options.audio_start = if self.imported_audio {
            midi::parse_time(self.start.trim())?
        } else {
            0.
        };
        Ok(options)
    }
    fn start_job<F>(&mut self, ctx: &egui::Context, kind: JobKind, task: F)
    where
        F: FnOnce(JobControl) -> Result<JobOutput> + Send + 'static,
    {
        if self.busy() || self.closing {
            return;
        }
        self.player.stop();
        self.error = None;
        self.fraction = 0.;
        self.next_job += 1;
        let id = self.next_job;
        let cancel = Arc::new(AtomicBool::new(false));
        let worker_cancel = cancel.clone();
        let tx = self.tx.clone();
        let progress_tx = tx.clone();
        let repaint = ctx.clone();
        let progress_repaint = repaint.clone();
        let thread = thread::spawn(move || {
            let control = JobControl {
                cancel: worker_cancel,
                progress: Arc::new(move |p| {
                    let _ = progress_tx.send(Event::Progress(id, p));
                    progress_repaint.request_repaint();
                }),
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| task(control)))
                .map_err(|_| "任务意外停止，请检查素材后重试。".to_owned())
                .and_then(|r| r.map_err(|e| format!("{e:#}")));
            let _ = tx.send(Event::Done(id, kind, result));
            repaint.request_repaint();
        });
        self.job = Some(ActiveJob { id, cancel, thread });
    }
    fn read_midi(&mut self, ctx: &egui::Context) {
        if self.midi_path.trim().is_empty() {
            self.fail("请选择 MIDI 文件。");
            return;
        }
        self.invalidate_preview();
        let path = absolute(Path::new(self.midi_path.trim()));
        self.status = "正在读取 MIDI…".into();
        self.start_job(ctx, JobKind::Read, move |c| {
            c.check()?;
            let stamp = file_stamp(&path)?;
            let data = midi::load(&path)?;
            c.check()?;
            if stamp != file_stamp(&path)? {
                bail!("读取期间 MIDI 文件发生变化，请重试。");
            }
            Ok(JobOutput::Read { path, stamp, data })
        });
    }
    fn inspect(&mut self, ctx: &egui::Context) {
        let request = (|| -> Result<_> {
            let data = self.selected_data()?;
            let path = PathBuf::from(if self.output_path.trim().is_empty() {
                "output.mp4"
            } else {
                self.output_path.trim()
            });
            Ok((data, self.make_options(path, false)?))
        })();
        let (data, options) = match request {
            Ok(v) => v,
            Err(e) => {
                self.fail(e);
                return;
            }
        };
        if options.audio_path.is_some() && options.visuals == VisualMode::Labels {
            self.error = None;
            self.status = "导入音乐＋音名画面无需提供音高素材，可以试听或导出。".into();
            return;
        }
        self.status = "正在检查素材和时长…".into();
        self.start_job(ctx, JobKind::Inspect, move |c| {
            Ok(JobOutput::Inspect(engine::inspect_materials(
                &data, &options, &c,
            )?))
        });
    }
    fn preview(&mut self, ctx: &egui::Context) {
        let request = (|| -> Result<_> {
            let data = self.selected_data()?;
            let directory = self
                .preview_temp
                .as_ref()
                .ok_or_else(|| anyhow!("无法创建试听缓存目录。"))?
                .path()
                .to_owned();
            let n = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            let mut options =
                self.make_options(directory.join(format!("selection-{n}.wav")), true)?;
            options.overwrite = false;
            Ok((data, options))
        })();
        let (data, options) = match request {
            Ok(v) => v,
            Err(e) => {
                self.fail(e);
                return;
            }
        };
        let cached = self.preview_cache.clone();
        self.status = "正在生成完整选区试听，可随时取消…".into();
        self.start_job(ctx, JobKind::Preview, move |c| {
            let key = preview_key(&data, &options)?;
            c.check()?;
            if let Some((old, path)) = cached {
                if old == key && path.is_file() {
                    return Ok(JobOutput::Preview { path, key });
                }
            }
            let path = engine::render_audio(&data, &options, &c)?;
            Ok(JobOutput::Preview { path, key })
        });
    }
    fn export(&mut self, ctx: &egui::Context, audio_only: bool) {
        let path = if audio_only {
            let base = Path::new(if self.output_path.trim().is_empty() {
                "output.mp4"
            } else {
                self.output_path.trim()
            });
            let name = format!(
                "{}_audio.wav",
                base.file_stem().unwrap_or_default().to_string_lossy()
            );
            rfd::FileDialog::new()
                .set_title("仅导出选区音频")
                .add_filter("WAV 音频", &["wav"])
                .set_file_name(name)
                .save_file()
        } else if self.output_path.trim().is_empty() {
            rfd::FileDialog::new()
                .set_title("导出 MP4 视频")
                .add_filter("MP4 视频", &["mp4"])
                .set_file_name("output.mp4")
                .save_file()
        } else {
            Some(PathBuf::from(self.output_path.trim()))
        };
        let Some(path) = path else {
            return;
        };
        let extension = if audio_only { "wav" } else { "mp4" };
        if path
            .extension()
            .and_then(|s| s.to_str())
            .map(|s| s.eq_ignore_ascii_case(extension))
            != Some(true)
        {
            self.fail(format!("保存文件名应以 .{extension} 结尾。"));
            return;
        }
        if path.is_dir() {
            self.fail("保存位置需要包含文件名。");
            return;
        }
        if !confirm_replace(&path) {
            return;
        }
        let request = (|| -> Result<_> {
            let data = self.selected_data()?;
            let mut options = self.make_options(path.clone(), audio_only)?;
            options.overwrite = path.exists();
            Ok((data, options))
        })();
        let (data, options) = match request {
            Ok(v) => v,
            Err(e) => {
                self.fail(e);
                return;
            }
        };
        if !audio_only {
            self.output_path = path.to_string_lossy().into_owned();
        }
        self.status = if audio_only {
            "正在导出 WAV 音频…".into()
        } else {
            "正在导出 MP4 视频…".into()
        };
        self.start_job(
            ctx,
            if audio_only {
                JobKind::Audio
            } else {
                JobKind::Video
            },
            move |c| {
                let path = if audio_only {
                    engine::render_audio(&data, &options, &c)?
                } else {
                    engine::render(&data, &options, &c)?
                };
                Ok(JobOutput::Export(path))
            },
        );
    }
    fn cancel(&mut self) {
        if let Some(job) = &self.job {
            job.cancel.store(true, Ordering::Relaxed);
            self.status = "正在停止任务并清理临时文件…".into();
        }
    }
    fn poll_events(&mut self, ctx: &egui::Context) {
        for _ in 0..300 {
            let event = match self.rx.try_recv() {
                Ok(v) => v,
                Err(_) => break,
            };
            match event {
                Event::Progress(id, p) => {
                    if self.job.as_ref().is_some_and(|j| j.id == id) {
                        self.fraction = self.fraction.max(p.fraction.clamp(0., 1.));
                        if !self.closing
                            && !self.job.as_ref().unwrap().cancel.load(Ordering::Relaxed)
                        {
                            self.status = p.message.clone();
                        }
                        self.log(p.message);
                    }
                }
                Event::Done(id, _kind, result) => {
                    if !self.job.as_ref().is_some_and(|j| j.id == id) {
                        continue;
                    }
                    let job = self.job.take().unwrap();
                    let cancelled = job.cancel.load(Ordering::Relaxed);
                    drop(job.thread);
                    if cancelled && !matches!(&result, Ok(JobOutput::Export(_))) {
                        if let Ok(JobOutput::Preview { path, .. }) = result {
                            if self.preview_cache.as_ref().map(|(_, p)| p) != Some(&path) {
                                let _ = std::fs::remove_file(path);
                            }
                        }
                        self.status = "已取消，原有输出文件未修改。".into();
                    } else {
                        self.finish(result);
                    }
                    if self.closing {
                        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                    }
                }
            }
        }
        match self.player.poll() {
            Ok(false) if self.status.starts_with("正在试听") => {
                self.status = "试听结束。".into()
            }
            Err(e) => {
                self.player.stop();
                self.fail(e);
            }
            _ => {}
        }
    }
    fn finish(&mut self, result: std::result::Result<JobOutput, String>) {
        match result {
            Err(e) => self.fail(e),
            Ok(JobOutput::Read { path, stamp, data }) => {
                self.selected_tracks = data
                    .tracks
                    .iter()
                    .filter(|t| t.note_count > 0)
                    .map(|t| t.index)
                    .collect();
                self.loaded_path = Some(path.clone());
                self.loaded_stamp = Some(stamp);
                if self.output_path.trim().is_empty() {
                    self.output_path = path
                        .with_extension("stitched.mp4")
                        .to_string_lossy()
                        .into_owned();
                }
                self.start = "0".into();
                self.end.clear();
                self.channel = None;
                self.infos.clear();
                self.selected_material = None;
                self.material_dirty = false;
                self.status = format!(
                    "MIDI 已读取：{} 个音符，时长 {}。",
                    data.notes.len(),
                    time_text(data.duration)
                );
                for warning in &data.warnings {
                    self.log(format!("提示：{warning}"));
                }
                self.data = Some(data);
            }
            Ok(JobOutput::Inspect(infos)) => {
                self.status = format!(
                    "素材检查通过：{} 种音高，可以设置片段、试听或导出。",
                    infos.len()
                );
                self.infos.extend(infos);
                if self.selected_material.is_none() {
                    if let Some(pitch) = self.needed_materials().keys().next().copied() {
                        let _ = self.choose_material(pitch);
                    }
                }
            }
            Ok(JobOutput::Preview { path, key }) => {
                if let Some((_, old)) = self.preview_cache.take() {
                    if old != path {
                        let _ = std::fs::remove_file(old);
                    }
                }
                self.preview_cache = Some((key, path.clone()));
                match self.player.play(&path) {
                    Ok(()) => {
                        self.status = "正在试听完整选区；修改设置会停止当前试听。".into();
                        self.fraction = 1.;
                    }
                    Err(e) => self.fail(e),
                }
            }
            Ok(JobOutput::Export(path)) => {
                self.status = format!("导出完成：{}", path.display());
                self.log(self.status.clone());
                self.last_output = Some(path);
                self.fraction = 1.;
            }
        }
    }
    fn file_controls(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.strong("MIDI 文件");
        ui.horizontal(|ui| {
            let mut path = self.midi_path.clone();
            if ui
                .add(
                    egui::TextEdit::singleline(&mut path)
                        .desired_width((ui.available_width() - 112.).max(140.))
                        .hint_text("选择 .mid 或 .midi 文件"),
                )
                .changed()
            {
                self.set_midi_path(path);
            }
            if ui.button("选择 MIDI").clicked() {
                if let Some(path) = rfd::FileDialog::new()
                    .add_filter("MIDI 文件", &["mid", "midi"])
                    .pick_file()
                {
                    self.set_midi_path(path.to_string_lossy().into_owned());
                    self.read_midi(ctx);
                }
            }
        });
        ui.add_space(6.);
        ui.strong("素材文件夹");
        ui.horizontal(|ui| {
            let mut folder = self.video_dir.clone();
            if ui
                .add(
                    egui::TextEdit::singleline(&mut folder)
                        .desired_width((ui.available_width() - 112.).max(140.))
                        .hint_text("每个音高对应一个视频或音频素材"),
                )
                .changed()
            {
                self.set_video_dir(folder);
            }
            if ui.button("选择目录").clicked() {
                if let Some(path) = rfd::FileDialog::new().pick_folder() {
                    self.set_video_dir(path.to_string_lossy().into_owned());
                }
            }
        });
        ui.add_space(6.);
        ui.horizontal_wrapped(|ui| {
            if ui.button("读取 MIDI").clicked() {
                self.read_midi(ctx);
            }
            if ui
                .add_enabled(self.data.is_some(), egui::Button::new("检查所选素材"))
                .clicked()
            {
                self.inspect(ctx);
            }
        });
        ui.label(
            RichText::new(
                "素材按音高命名，例如 C4.mp4、C#4.wav 或 60.mp4。导入完整音乐时可不选素材。",
            )
            .size(13.)
            .color(MUTED),
        );
    }
    fn selection_controls(&mut self, ui: &mut egui::Ui) {
        let Some(data) = self.data.as_ref() else {
            egui::Frame::new()
                .fill(BACKGROUND)
                .corner_radius(8)
                .inner_margin(18)
                .show(ui, |ui| {
                    ui.label(RichText::new("等待导入 MIDI").size(16.).strong());
                    ui.label(
                        RichText::new("读取后，这里会显示轨道、力度和可拖动的时间轴。")
                            .color(MUTED),
                    );
                });
            return;
        };
        let tracks = data.tracks.clone();
        let duration = data.duration;
        let mut changed = false;
        ui.horizontal(|ui| {
            if ui.button("全选轨道").clicked() {
                self.selected_tracks = tracks
                    .iter()
                    .filter(|t| t.note_count > 0)
                    .map(|t| t.index)
                    .collect();
                changed = true;
            }
            if ui.button("清空").clicked() {
                self.selected_tracks.clear();
                changed = true;
            }
            egui::ComboBox::from_id_salt("channel")
                .selected_text(
                    self.channel
                        .map(|c| format!("通道 {}", c + 1))
                        .unwrap_or_else(|| "全部通道".into()),
                )
                .show_ui(ui, |ui| {
                    changed |= ui
                        .selectable_value(&mut self.channel, None, "全部通道")
                        .changed();
                    for c in 0..16 {
                        changed |= ui
                            .selectable_value(&mut self.channel, Some(c), format!("通道 {}", c + 1))
                            .changed();
                    }
                });
        });
        egui::ScrollArea::vertical()
            .id_salt("tracks")
            .max_height(125.)
            .show(ui, |ui| {
                for track in tracks {
                    if track.note_count == 0 {
                        continue;
                    }
                    let mut selected = self.selected_tracks.contains(&track.index);
                    if ui
                        .checkbox(
                            &mut selected,
                            format!(
                                "[{}] {}  ·  {} 个音符",
                                track.index, track.name, track.note_count
                            ),
                        )
                        .changed()
                    {
                        if selected {
                            self.selected_tracks.insert(track.index);
                        } else {
                            self.selected_tracks.remove(&track.index);
                        }
                        changed = true;
                    }
                }
            });
        ui.horizontal_wrapped(|ui| {
            ui.label("开始");
            changed |= ui
                .add(egui::TextEdit::singleline(&mut self.start).desired_width(100.))
                .changed();
            ui.label("结束");
            changed |= ui
                .add(
                    egui::TextEdit::singleline(&mut self.end)
                        .desired_width(100.)
                        .hint_text("留空为全曲"),
                )
                .changed();
            if ui.button("选择全曲").clicked() {
                self.start = "0".into();
                self.end.clear();
                changed = true;
            }
            ui.label(
                RichText::new(format!("总时长 {} · 支持秒、分:秒", time_text(duration)))
                    .color(MUTED),
            );
        });
        changed |= self.timeline(ui);
        if changed {
            self.invalidate_preview();
        }
        match self.selected_data() {
            Ok(data) => {
                let pitches: BTreeSet<_> = data.notes.iter().map(|n| n.pitch).collect();
                let min = data.notes.iter().map(|n| n.velocity).min().unwrap_or(0);
                let max = data.notes.iter().map(|n| n.velocity).max().unwrap_or(0);
                ui.label(format!(
                    "选区 {} · {} 个音符 / {} 种音高 · 力度 {}–{} / 127",
                    time_text(data.duration),
                    data.notes.len(),
                    pitches.len(),
                    min,
                    max
                ));
            }
            Err(e) => {
                ui.colored_label(ui.visuals().error_fg_color, e.to_string());
            }
        }
    }
    fn timeline(&mut self, ui: &mut egui::Ui) -> bool {
        let Some(data) = self.data.as_ref() else {
            return false;
        };
        let duration = data.duration;
        if duration <= 0. {
            return false;
        }
        let (rect, response) = ui.allocate_exact_size(
            egui::vec2(ui.available_width(), 85.),
            Sense::click_and_drag(),
        );
        let painter = ui.painter_at(rect);
        painter.rect_filled(rect, 8., Color32::from_rgb(246, 246, 253));
        let left = rect.left() + 10.;
        let width = (rect.width() - 20.).max(1.);
        let mut start = midi::parse_time(self.start.trim())
            .unwrap_or(0.)
            .clamp(0., duration);
        let mut end = if self.end.trim().is_empty() {
            duration
        } else {
            midi::parse_time(self.end.trim())
                .unwrap_or(duration)
                .clamp(start, duration)
        };
        let position = |time: f64| left + width * (time / duration) as f32;
        painter.rect_filled(
            egui::Rect::from_min_max(
                egui::pos2(position(start), rect.top() + 5.),
                egui::pos2(position(end), rect.bottom() - 18.),
            ),
            0.,
            Color32::from_rgb(230, 227, 255),
        );
        let mut density = vec![0.0f32; 250];
        for note in &data.notes {
            if self.selected_tracks.contains(&note.track)
                && self.channel.is_none_or(|c| c == note.channel)
            {
                let i = ((note.start / duration * density.len() as f64) as usize)
                    .min(density.len() - 1);
                density[i] += note.velocity as f32 / 127.;
            }
        }
        let max = density.iter().copied().fold(0.0f32, f32::max);
        if max > 0. {
            for (i, n) in density.iter().enumerate() {
                if *n <= 0. {
                    continue;
                }
                let x = left + width * i as f32 / density.len() as f32;
                painter.rect_filled(
                    egui::Rect::from_min_max(
                        egui::pos2(x, rect.bottom() - 20. - n / max * 48.),
                        egui::pos2(
                            x + (width / density.len() as f32).max(1.),
                            rect.bottom() - 20.,
                        ),
                    ),
                    0.,
                    if x >= position(start) && x <= position(end) {
                        BLUE
                    } else {
                        MUTED
                    },
                );
            }
        }
        for time in [start, end] {
            let x = position(time);
            painter.line_segment(
                [
                    egui::pos2(x, rect.top() + 4.),
                    egui::pos2(x, rect.bottom() - 17.),
                ],
                egui::Stroke::new(3.0_f32, BLUE),
            );
        }
        painter.text(
            egui::pos2(left, rect.bottom() - 2.),
            egui::Align2::LEFT_BOTTOM,
            "0:00",
            egui::FontId::proportional(11.),
            MUTED,
        );
        painter.text(
            egui::pos2(rect.right() - 10., rect.bottom() - 2.),
            egui::Align2::RIGHT_BOTTOM,
            time_text(duration),
            egui::FontId::proportional(11.),
            MUTED,
        );
        let mut changed = false;
        if let Some(pointer) = response.interact_pointer_pos() {
            let time =
                (((pointer.x - left) / width).clamp(0., 1.) as f64 * duration).clamp(0., duration);
            if response.drag_started() {
                self.drag = Some(if (pointer.x - position(start)).abs() < 15. {
                    Boundary::Start
                } else if (pointer.x - position(end)).abs() < 15. {
                    Boundary::End
                } else {
                    Boundary::New(time)
                });
            }
            if response.dragged() {
                let span = duration.min(0.001);
                match self.drag {
                    Some(Boundary::Start) => start = time.min((end - span).max(0.)),
                    Some(Boundary::End) => end = time.max((start + span).min(duration)),
                    Some(Boundary::New(anchor)) => {
                        start = anchor.min(time);
                        end = anchor.max(time).max((start + span).min(duration));
                    }
                    None => {}
                }
                self.start = time_text(start);
                self.end = if end >= duration - 0.000001 {
                    String::new()
                } else {
                    time_text(end)
                };
                changed = true;
            }
        }
        if response.drag_stopped() {
            self.drag = None;
        }
        changed
    }
    fn material_controls(&mut self, ui: &mut egui::Ui) {
        if self.imported_audio {
            ui.label("当前使用整段音乐；切回素材原声后继续使用原来的选段设置。");
            return;
        }
        let needed = self.needed_materials();
        if needed.is_empty() {
            ui.label("选择 MIDI 选区后显示所需素材。");
            return;
        }
        let mut selected = None;
        egui::ScrollArea::vertical()
            .id_salt("materials")
            .max_height(205.)
            .show(ui, |ui| {
                egui::Grid::new("material-grid")
                    .striped(true)
                    .num_columns(6)
                    .spacing([18., 7.])
                    .show(ui, |ui| {
                        for heading in ["音高", "素材时长", "最长需求", "方式", "开始", "结束"]
                        {
                            ui.strong(heading);
                        }
                        ui.end_row();
                        for (pitch, required) in &needed {
                            let choice = self
                                .options
                                .materials
                                .get(pitch)
                                .cloned()
                                .unwrap_or_default();
                            if ui
                                .selectable_label(
                                    self.selected_material == Some(*pitch),
                                    note_name(*pitch),
                                )
                                .clicked()
                            {
                                selected = Some(*pitch);
                            }
                            ui.label(
                                self.infos
                                    .get(pitch)
                                    .map(|i| format!("{:.3} s", i.duration))
                                    .unwrap_or_else(|| "待检查".into()),
                            );
                            ui.label(format!("{required:.3} s"));
                            ui.label(if choice.random { "随机" } else { "固定" });
                            ui.label(format!("{:.3}", choice.start));
                            ui.label(
                                choice
                                    .end
                                    .map(|v| format!("{v:.3}"))
                                    .unwrap_or_else(|| "素材末尾".into()),
                            );
                            ui.end_row();
                        }
                    });
            });
        if let Some(pitch) = selected {
            if let Err(e) = self.choose_material(pitch) {
                self.fail(e);
            }
        }
        ui.add_enabled_ui(
            self.selected_material
                .is_some_and(|pitch| self.infos.contains_key(&pitch)),
            |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.label(
                        self.selected_material
                            .map(|p| format!("编辑 {}", note_name(p)))
                            .unwrap_or_else(|| "选择一行编辑".into()),
                    );
                    self.material_dirty |=
                        ui.checkbox(&mut self.material_random, "随机选段").changed();
                    ui.label("开始");
                    self.material_dirty |= ui
                        .add(
                            egui::TextEdit::singleline(&mut self.material_start).desired_width(80.),
                        )
                        .changed();
                    ui.label("结束");
                    self.material_dirty |= ui
                        .add(
                            egui::TextEdit::singleline(&mut self.material_end)
                                .desired_width(100.)
                                .hint_text("留空为末尾"),
                        )
                        .changed();
                    if ui.button("应用到此素材").clicked() {
                        if let Some(pitch) = self.selected_material {
                            if let Err(e) = self.apply_material(&[pitch]) {
                                self.fail(e);
                            }
                        }
                    }
                    if ui.button("应用到全部素材").clicked() {
                        if let Err(e) =
                            self.apply_material(&needed.keys().copied().collect::<Vec<_>>())
                        {
                            self.fail(e);
                        }
                    }
                    if ui.button("重置此素材").clicked() {
                        if let Some(pitch) = self.selected_material {
                            self.options.materials.remove(&pitch);
                            self.selected_material = None;
                            self.material_dirty = false;
                            let _ = self.choose_material(pitch);
                            self.invalidate_preview();
                        }
                    }
                });
            },
        );
        ui.horizontal_wrapped(|ui| {
            ui.label("随机编号");
            if ui
                .add(egui::DragValue::new(&mut self.options.seed).speed(1.))
                .changed()
            {
                self.invalidate_preview();
            }
            if ui.button("重新随机").clicked() {
                self.options.seed = self.options.seed.wrapping_add(1);
                self.invalidate_preview();
            }
            ui.label(RichText::new("同一编号下，试听与导出使用相同片段。").color(MUTED));
        });
        ui.label(
            RichText::new(
                "例如 5 秒素材可选 1–3 秒；随机选取足够长的片段，区间不足时在区间内循环。",
            )
            .color(MUTED),
        );
        if self.material_dirty {
            self.player.stop();
        }
    }
    fn sound_controls(&mut self, ui: &mut egui::Ui) {
        ui.strong("声音来源");
        let mut changed = false;
        ui.horizontal_wrapped(|ui| {
            changed |= ui
                .selectable_value(&mut self.imported_audio, false, "素材原声混音")
                .changed();
            if ui
                .selectable_value(&mut self.imported_audio, true, "导入整段音乐")
                .changed()
            {
                changed = true;
                self.options.visuals = VisualMode::Labels;
            }
        });
        ui.add_space(8.);
        ui.strong("画面模式");
        ui.horizontal_wrapped(|ui| {
            egui::ComboBox::from_id_salt("visual-mode")
                .width(250.)
                .selected_text(if self.options.visuals == VisualMode::Labels {
                    "音名 / 频率 / 力度（快速）"
                } else {
                    "原视频素材分屏"
                })
                .show_ui(ui, |ui| {
                    changed |= ui
                        .selectable_value(
                            &mut self.options.visuals,
                            VisualMode::Labels,
                            "音名 / 频率 / 力度（快速）",
                        )
                        .changed();
                    changed |= ui
                        .selectable_value(
                            &mut self.options.visuals,
                            VisualMode::Clips,
                            "原视频素材分屏",
                        )
                        .changed();
                });
        });
        if self.imported_audio {
            ui.horizontal(|ui| {
                ui.label("音乐文件");
                changed |= ui
                    .add(
                        egui::TextEdit::singleline(&mut self.audio_path)
                            .desired_width(ui.available_width() - 115.),
                    )
                    .changed();
                if ui.button("选择音频").clicked() {
                    if let Some(path) = rfd::FileDialog::new()
                        .add_filter("音频", &["wav", "flac", "mp3", "m4a", "ogg", "aac", "mp4"])
                        .pick_file()
                    {
                        self.audio_path = path.to_string_lossy().into_owned();
                        self.options.visuals = VisualMode::Labels;
                        changed = true;
                    }
                }
            });
            ui.label(
                RichText::new(
                    "音乐与 MIDI 原始 0 秒对齐，选区自动截取对应音乐；不按音符力度修改整段音乐。",
                )
                .color(MUTED),
            );
        }
        ui.horizontal_wrapped(|ui| {
            ui.add_enabled_ui(!self.imported_audio, |ui| {
                changed |= ui
                    .checkbox(&mut self.options.velocity, "按 MIDI 力度控制音量")
                    .changed();
            });
            ui.label(RichText::new("127＝原音量，64≈一半；画面显示每个音符的力度。").color(MUTED));
        });
        ui.horizontal_wrapped(|ui| {
            ui.label("分辨率");
            let (w, h) = (self.options.width, self.options.height);
            egui::ComboBox::from_id_salt("resolution")
                .selected_text(format!("{w} × {h}"))
                .show_ui(ui, |ui| {
                    for (width, height, label) in [
                        (854, 480, "480p · 854 × 480"),
                        (1280, 720, "720p · 1280 × 720"),
                        (1920, 1080, "1080p · 1920 × 1080"),
                    ] {
                        if ui
                            .selectable_label(w == width && h == height, label)
                            .clicked()
                        {
                            self.options.width = width;
                            self.options.height = height;
                            changed = true;
                        }
                    }
                });
            ui.label("帧率");
            egui::ComboBox::from_id_salt("fps")
                .selected_text(self.options.fps.to_string())
                .show_ui(ui, |ui| {
                    for value in [24, 25, 30, 50, 60] {
                        changed |= ui
                            .selectable_value(&mut self.options.fps, value, value.to_string())
                            .changed();
                    }
                });
            let text = match self.options.chord {
                ChordPolicy::All => "全部同时音符",
                ChordPolicy::Highest => "仅最高音",
                ChordPolicy::Lowest => "仅最低音",
                ChordPolicy::Latest => "最后进入的音符",
            };
            egui::ComboBox::from_id_salt("chord")
                .selected_text(text)
                .show_ui(ui, |ui| {
                    for (value, label) in [
                        (ChordPolicy::All, "全部同时音符"),
                        (ChordPolicy::Highest, "仅最高音"),
                        (ChordPolicy::Lowest, "仅最低音"),
                        (ChordPolicy::Latest, "最后进入的音符"),
                    ] {
                        changed |= ui
                            .selectable_value(&mut self.options.chord, value, label)
                            .changed();
                    }
                });
        });
        egui::CollapsingHeader::new("更多设置").show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.label("并行任务（0 为自动）");
                changed |= ui
                    .add(egui::DragValue::new(&mut self.options.workers).range(0..=32))
                    .changed();
                ui.label("FFmpeg 路径");
                let mut value = self
                    .options
                    .ffmpeg
                    .as_ref()
                    .map(|p| p.to_string_lossy().into_owned())
                    .unwrap_or_default();
                if ui
                    .add(
                        egui::TextEdit::singleline(&mut value)
                            .desired_width(280.)
                            .hint_text("自动查找，可留空"),
                    )
                    .changed()
                {
                    self.options.ffmpeg = if value.trim().is_empty() {
                        None
                    } else {
                        Some(value.trim().into())
                    };
                    changed = true;
                }
                if ui.button("选择 FFmpeg").clicked() {
                    if let Some(path) = rfd::FileDialog::new().pick_file() {
                        self.options.ffmpeg = Some(path);
                        changed = true;
                    }
                }
            });
        });
        if changed {
            self.invalidate_preview();
        }
    }
    fn output_controls(&mut self, ui: &mut egui::Ui) {
        ui.strong("MP4 保存位置");
        ui.horizontal(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.output_path)
                    .desired_width((ui.available_width() - 112.).max(140.))
                    .hint_text("选择成片的保存位置"),
            );
            if ui.button("选择位置").clicked() {
                if let Some(path) = rfd::FileDialog::new()
                    .add_filter("MP4 视频", &["mp4"])
                    .set_file_name("output.mp4")
                    .save_file()
                {
                    self.output_path = path.to_string_lossy().into_owned();
                }
            }
        });
        ui.label(
            RichText::new("单独导出 WAV 时会另选保存位置；已有文件会在覆盖前提示。")
                .size(13.)
                .color(MUTED),
        );
    }
    fn header(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(egui::vec2(40., 40.), Sense::hover());
            let painter = ui.painter();
            painter.rect_filled(rect, 10., BLUE);
            for (i, height) in [12., 23., 18., 29., 12.].iter().enumerate() {
                let x = rect.left() + 10. + i as f32 * 4.8;
                painter.line_segment(
                    [
                        egui::pos2(x, rect.center().y - height / 2.),
                        egui::pos2(x, rect.center().y + height / 2.),
                    ],
                    egui::Stroke::new(2.4_f32, Color32::WHITE),
                );
            }
            ui.add_space(3.);
            ui.vertical(|ui| {
                ui.label(
                    RichText::new("MIDI 视频工作室")
                        .size(22.)
                        .strong()
                        .color(INK),
                );
                ui.label(
                    RichText::new("让每一个音符，都有自己的画面")
                        .size(12.)
                        .color(MUTED),
                );
            });
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                egui::Frame::new()
                    .fill(TINT)
                    .corner_radius(6)
                    .inner_margin(egui::Margin::symmetric(10, 5))
                    .show(ui, |ui| {
                        ui.label(
                            RichText::new(format!("Rust · v{}", env!("CARGO_PKG_VERSION")))
                                .size(12.)
                                .color(BLUE)
                                .strong(),
                        );
                    });
            });
        });
        ui.add_space(14.);
        let width = ((ui.available_width() - 18.) / 3.).max(150.);
        ui.horizontal(|ui| {
            for (page, label) in [
                (Workspace::Input, "01   导入与选区"),
                (Workspace::Materials, "02   素材片段"),
                (Workspace::Output, "03   声音与输出"),
            ] {
                let active = self.workspace == page;
                let button = egui::Button::new(RichText::new(label).strong().color(if active {
                    BLUE
                } else {
                    MUTED
                }))
                .fill(if active { TINT } else { Color32::WHITE })
                .stroke(egui::Stroke::new(
                    1_f32,
                    if active {
                        BLUE.gamma_multiply(0.25)
                    } else {
                        BORDER
                    },
                ))
                .min_size(egui::vec2(width, 39.));
                if ui.add(button).clicked() {
                    self.workspace = page;
                }
            }
        });
    }
    fn project_summary(&self, ui: &mut egui::Ui) {
        ui.label(RichText::new("项目概览").size(16.).strong());
        ui.add_space(12.);
        let filename = Path::new(&self.midi_path)
            .file_name()
            .map(|p| p.to_string_lossy().into_owned())
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| "尚未选择 MIDI".into());
        ui.label(RichText::new(filename).strong());
        ui.add_space(6.);
        if let Ok(data) = self.selected_data() {
            egui::Frame::new()
                .fill(BACKGROUND)
                .corner_radius(9)
                .inner_margin(12)
                .show(ui, |ui| {
                    ui.label(
                        RichText::new(time_text(data.duration))
                            .size(25.)
                            .strong()
                            .color(BLUE),
                    );
                    ui.label(RichText::new("当前选区时长").small().color(MUTED));
                    ui.add_space(10.);
                    ui.label(format!("{} 个音符", data.notes.len()));
                    ui.label(format!("{} 条所选轨道", self.selected_tracks.len()));
                    let pitches: BTreeSet<_> = data.notes.iter().map(|n| n.pitch).collect();
                    ui.label(format!("{} 种音高", pitches.len()));
                });
        } else {
            ui.label(RichText::new("导入 MIDI 后，查看选区和导出信息。").color(MUTED));
        }
        ui.add_space(18.);
        ui.separator();
        ui.add_space(12.);
        ui.label(RichText::new("导出设置").strong());
        ui.add_space(6.);
        for (label, value) in [
            (
                "画面",
                if self.options.visuals == VisualMode::Clips {
                    "原视频分屏".into()
                } else {
                    "音名 / 频率 / 力度".into()
                },
            ),
            (
                "声音",
                if self.imported_audio {
                    "导入的完整音乐".into()
                } else {
                    "素材原声混音".into()
                },
            ),
            (
                "尺寸",
                format!("{} × {}", self.options.width, self.options.height),
            ),
            ("帧率", format!("{} fps", self.options.fps)),
        ] {
            ui.label(RichText::new(label).size(12.).color(MUTED));
            ui.label(value);
            ui.add_space(5.);
        }
        ui.add_space(16.);
        egui::Frame::new()
            .fill(TINT)
            .corner_radius(9)
            .inner_margin(12)
            .show(ui, |ui| {
                ui.label(RichText::new("小提示").color(BLUE).strong());
                ui.label(
                    RichText::new(match self.workspace {
                        Workspace::Input => "拖动时间轴选择片段，左右边线可调整起止位置。",
                        Workspace::Materials => {
                            "固定选段或随机选段都可试听；相同随机编号会复用相同片段。"
                        }
                        Workspace::Output => "重叠音符会同时分屏，声音按照 MIDI 力度叠加。",
                    })
                    .size(13.)
                    .color(MUTED),
                );
            });
    }
    fn workspace_contents(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.add_enabled_ui(!self.busy() && !self.closing, |ui| match self.workspace {
            Workspace::Input => {
                card(
                    ui,
                    "导入你的音乐项目",
                    "先选择 MIDI，再选择对应的声音或视频素材。",
                    |ui| self.file_controls(ui, ctx),
                );
                card(
                    ui,
                    "轨道与时间选区",
                    "只制作想听的声部与片段，支持多个轨道同时选择。",
                    |ui| self.selection_controls(ui),
                );
            }
            Workspace::Materials => {
                card(
                    ui,
                    "为每个音高挑选片段",
                    "点击素材行，设置固定范围或随机片段。",
                    |ui| {
                        ui.horizontal_wrapped(|ui| {
                            if ui
                                .add_enabled(self.data.is_some(), egui::Button::new("检查所选素材"))
                                .clicked()
                            {
                                self.inspect(ctx);
                            }
                            ui.label(
                                RichText::new(format!("已检查 {} 种素材", self.infos.len()))
                                    .small()
                                    .color(MUTED),
                            );
                        });
                        ui.add_space(12.);
                        self.material_controls(ui);
                    },
                );
            }
            Workspace::Output => {
                card(
                    ui,
                    "声音与画面",
                    "保留素材原声，或导入完整音乐制作音名画面。",
                    |ui| self.sound_controls(ui),
                );
                card(
                    ui,
                    "保存成片",
                    "设置保存位置，试听满意后即可导出。",
                    |ui| self.output_controls(ui),
                );
            }
        });
        ui.horizontal(|ui| {
            if self.workspace != Workspace::Input && ui.button("上一步").clicked() {
                self.workspace = if self.workspace == Workspace::Output {
                    Workspace::Materials
                } else {
                    Workspace::Input
                };
            }
            if self.workspace != Workspace::Output {
                let text = if self.workspace == Workspace::Input {
                    "下一步：素材片段"
                } else {
                    "下一步：声音与输出"
                };
                if ui.add(primary_button(text)).clicked() {
                    self.workspace = if self.workspace == Workspace::Input {
                        Workspace::Materials
                    } else {
                        Workspace::Output
                    };
                }
            }
        });
        ui.add_space(14.);
        egui::CollapsingHeader::new(RichText::new("处理记录").size(13.).color(MUTED)).show(
            ui,
            |ui| {
                egui::ScrollArea::vertical()
                    .id_salt("log")
                    .max_height(180.)
                    .stick_to_bottom(true)
                    .show(ui, |ui| {
                        for line in &self.logs {
                            ui.label(RichText::new(line).small());
                        }
                    });
            },
        );
    }
    fn show_workspace(&mut self, ctx: &egui::Context) -> (egui::Rect, egui::Rect) {
        let mut footer = egui::Rect::NOTHING;
        egui::TopBottomPanel::bottom("actions")
            .frame(
                egui::Frame::new()
                    .fill(Color32::WHITE)
                    .stroke(egui::Stroke::new(1_f32, BORDER))
                    .inner_margin(egui::Margin::symmetric(20, 12)),
            )
            .show(ctx, |ui| {
                self.footer(ui, ctx);
                footer = ui.min_rect();
            });
        egui::TopBottomPanel::top("workspace-header")
            .frame(
                egui::Frame::new()
                    .fill(Color32::WHITE)
                    .inner_margin(egui::Margin::symmetric(22, 16)),
            )
            .show(ctx, |ui| self.header(ui));
        if ctx.content_rect().width() >= 1080. {
            egui::SidePanel::right("project-summary")
                .resizable(false)
                .exact_width(244.)
                .frame(egui::Frame::new().fill(Color32::WHITE).inner_margin(18))
                .show(ctx, |ui| {
                    egui::ScrollArea::vertical()
                        .id_salt("summary-scroll")
                        .show(ui, |ui| self.project_summary(ui));
                });
        }
        let mut content = egui::Rect::NOTHING;
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BACKGROUND).inner_margin(18))
            .show(ctx, |ui| {
                egui::ScrollArea::vertical()
                    .id_salt(("workspace-content", self.workspace as u8))
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        self.workspace_contents(ui, ctx);
                        content = ui.min_rect();
                    });
            });
        (footer, content)
    }
    fn footer(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.horizontal_wrapped(|ui| {
            let ready = !self.busy() && !self.closing && self.data.is_some();
            if ui
                .add_enabled(
                    ready,
                    egui::Button::new("试听选区").min_size(egui::vec2(100., 38.)),
                )
                .clicked()
            {
                self.preview(ctx);
            }
            if ui
                .add_enabled(self.player.active, egui::Button::new("停止试听"))
                .clicked()
            {
                self.player.stop();
                self.status = "试听已停止。".into();
            }
            if ui
                .add_enabled(ready, egui::Button::new("导出音频 WAV"))
                .clicked()
            {
                self.export(ctx, true);
            }
            if ui
                .add_enabled(ready, primary_button("导出视频 MP4"))
                .clicked()
            {
                self.export(ctx, false);
            }
            if ui
                .add_enabled(self.busy() && !self.closing, egui::Button::new("取消任务"))
                .clicked()
            {
                self.cancel();
            }
            if ui
                .add_enabled(
                    !self.busy() && self.last_output.is_some(),
                    egui::Button::new("打开结果"),
                )
                .clicked()
            {
                if let Some(path) = self.last_output.clone() {
                    if let Err(e) = open_path(&path) {
                        self.fail(e);
                    }
                }
            }
            if ui
                .add_enabled(
                    !self.busy() && self.last_output.is_some(),
                    egui::Button::new("打开目录"),
                )
                .clicked()
            {
                if let Some(path) = self
                    .last_output
                    .as_ref()
                    .and_then(|p| p.parent())
                    .map(Path::to_owned)
                {
                    if let Err(e) = open_path(&path) {
                        self.fail(e);
                    }
                }
            }
        });
        ui.add_space(3.);
        if self.busy() {
            ui.add(
                egui::ProgressBar::new(self.fraction as f32)
                    .fill(BLUE)
                    .desired_height(5.),
            );
        }
        ui.horizontal(|ui| {
            let color = if self.error.is_some() {
                ui.visuals().error_fg_color
            } else if self.busy() {
                BLUE
            } else {
                Color32::from_rgb(60, 146, 118)
            };
            ui.label(RichText::new("●").color(color).size(10.));
            if self.busy() {
                ui.label(
                    RichText::new(format!("{:.0}%", self.fraction * 100.))
                        .small()
                        .color(BLUE),
                );
            }
            ui.add(egui::Label::new(RichText::new(&self.status).size(12.).color(MUTED)).truncate())
                .on_hover_text(&self.status);
        });
        if let Some(error) = self.error.clone() {
            egui::Frame::new()
                .fill(Color32::from_rgb(255, 241, 242))
                .corner_radius(8)
                .inner_margin(10)
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(
                            RichText::new("操作未完成")
                                .strong()
                                .color(ui.visuals().error_fg_color),
                        );
                        if ui.small_button("复制错误详情").clicked() {
                            ctx.copy_text(error.clone());
                        }
                        if ui.small_button("关闭提示").clicked() {
                            self.error = None;
                        }
                    });
                    egui::CollapsingHeader::new("查看错误详情")
                        .id_salt("error-details-header")
                        .show(ui, |ui| {
                            egui::ScrollArea::vertical()
                                .id_salt("error-details")
                                .max_height(80.)
                                .show(ui, |ui| {
                                    ui.label(
                                        RichText::new(error)
                                            .size(12.)
                                            .color(ui.visuals().error_fg_color),
                                    );
                                });
                        });
                });
        }
    }
}
impl eframe::App for StitcherApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll_events(ctx);
        if self.pending_read {
            self.pending_read = false;
            self.read_midi(ctx);
        }
        if ctx.input(|i| i.viewport().close_requested()) && self.busy() {
            self.closing = true;
            self.player.stop();
            self.cancel();
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
        }
        self.show_workspace(ctx);
        if self.busy() || self.player.active {
            ctx.request_repaint_after(Duration::from_millis(100));
        }
    }
}
impl Drop for StitcherApp {
    fn drop(&mut self) {
        self.player.stop();
        if let Some(job) = self.job.take() {
            job.cancel.store(true, Ordering::Relaxed);
            let _ = job.thread.join();
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn loaded_app(directory: &Path) -> StitcherApp {
        let path = directory.join("fixture.mid");
        std::fs::write(&path, b"MIDI fixture metadata").unwrap();
        let mut app = StitcherApp::new_state(None);
        app.loaded_stamp = Some(file_stamp(&path).unwrap());
        app.loaded_path = Some(path.clone());
        app.midi_path = path.to_string_lossy().into_owned();
        app.selected_tracks.insert(0);
        app.data = Some(MidiData {
            notes: vec![
                Note {
                    pitch: 60,
                    start: 0.,
                    end: 2.,
                    velocity: 64,
                    channel: 0,
                    track: 0,
                    source_offset: 0.,
                },
                Note {
                    pitch: 64,
                    start: 1.,
                    end: 3.,
                    velocity: 127,
                    channel: 0,
                    track: 0,
                    source_offset: 0.,
                },
            ],
            tracks: vec![TrackInfo {
                index: 0,
                name: "test".into(),
                channels: vec![0],
                note_count: 2,
            }],
            duration: 4.,
            warnings: vec![],
        });
        app
    }
    #[test]
    fn player_starts_silent() {
        let mut player = PreviewPlayer::default();
        assert!(!player.active);
        assert!(!player.poll().unwrap());
        assert!(player.deadline.is_none());
    }
    #[test]
    fn material_ranges_validate_before_assignment() {
        let info = MediaInfo {
            path: "C4.mp4".into(),
            duration: 5.,
            has_video: true,
            has_audio: true,
        };
        assert!(validate_material(
            &MaterialChoice {
                start: 1.,
                end: Some(3.),
                random: true
            },
            60,
            &info
        )
        .is_ok());
        for (start, end) in [
            (-1., Some(2.)),
            (3., Some(2.)),
            (0., Some(5.1)),
            (f64::NAN, None),
        ] {
            assert!(validate_material(
                &MaterialChoice {
                    start,
                    end,
                    random: false
                },
                60,
                &info
            )
            .is_err());
        }
    }
    #[test]
    fn preview_cache_tracks_sources_not_output_names() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("song.wav");
        std::fs::write(&source, b"first").unwrap();
        let data = MidiData {
            notes: vec![Note {
                pitch: 60,
                start: 0.,
                end: 1.,
                velocity: 64,
                channel: 0,
                track: 0,
                source_offset: 0.,
            }],
            tracks: vec![],
            duration: 1.,
            warnings: vec![],
        };
        let mut options = RenderOptions {
            audio_path: Some(source.clone()),
            ..Default::default()
        };
        let first = preview_key(&data, &options).unwrap();
        options.output = "another.wav".into();
        options.overwrite = true;
        assert_eq!(first, preview_key(&data, &options).unwrap());
        options.velocity = false;
        assert_ne!(first, preview_key(&data, &options).unwrap());
        options.velocity = true;
        std::fs::write(source, b"updated longer").unwrap();
        assert_ne!(first, preview_key(&data, &options).unwrap());
    }
    #[test]
    fn time_boundary_does_not_round_past_end() {
        assert_eq!(time_text(1.999999), "0:01.999");
        assert_eq!(time_text(65.1234), "1:05.123");
    }
    #[cfg(target_os = "windows")]
    #[test]
    fn explorer_receives_normal_paths_after_canonicalized_overwrite() {
        assert_eq!(
            shell_path(Path::new(r"\\?\C:\音乐\finished.mp4")),
            PathBuf::from(r"C:\音乐\finished.mp4")
        );
        assert_eq!(
            shell_path(Path::new(r"\\?\UNC\server\share\finished.wav")),
            PathBuf::from(r"\\server\share\finished.wav")
        );
    }
    #[test]
    fn imported_music_options_follow_global_selection_without_materials() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = loaded_app(dir.path());
        let source = dir.path().join("song.wav");
        std::fs::write(&source, b"fixture").unwrap();
        app.imported_audio = true;
        app.audio_path = source.to_string_lossy().into_owned();
        app.start = "1".into();
        app.end = "2".into();
        let options = app
            .make_options(dir.path().join("result.mp4"), false)
            .unwrap();
        assert_eq!(options.visuals, VisualMode::Labels);
        assert_eq!(options.audio_path, Some(absolute(&source)));
        assert_eq!(options.audio_start, 1.);
        assert_eq!(app.selected_data().unwrap().duration, 1.);
        app.options.visuals = VisualMode::Clips;
        assert!(app.make_options(dir.path().join("only.wav"), true).is_ok());
        assert!(app
            .make_options(dir.path().join("video.mp4"), false)
            .is_err());
    }
    #[test]
    fn bulk_material_edits_validate_every_material_before_mutation() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = loaded_app(dir.path());
        app.infos.insert(
            60,
            MediaInfo {
                path: "C4.wav".into(),
                duration: 5.,
                has_video: false,
                has_audio: true,
            },
        );
        app.infos.insert(
            64,
            MediaInfo {
                path: "E4.wav".into(),
                duration: 1.,
                has_video: false,
                has_audio: true,
            },
        );
        app.material_start = "0.5".into();
        app.material_end = "2".into();
        app.material_random = true;
        assert!(app.apply_material(&[60, 64]).is_err());
        assert!(app.options.materials.is_empty());
        app.apply_material(&[60]).unwrap();
        assert_eq!(
            app.options.materials[&60],
            MaterialChoice {
                start: 0.5,
                end: Some(2.),
                random: true
            }
        );
    }
    #[test]
    fn cancelled_preview_never_plays_and_removes_the_generated_file() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = loaded_app(dir.path());
        let output = dir.path().join("preview.wav");
        std::fs::write(&output, b"not a playable WAV").unwrap();
        app.job = Some(ActiveJob {
            id: 7,
            cancel: Arc::new(AtomicBool::new(true)),
            thread: thread::spawn(|| {}),
        });
        app.tx
            .send(Event::Done(
                7,
                JobKind::Preview,
                Ok(JobOutput::Preview {
                    path: output.clone(),
                    key: 42,
                }),
            ))
            .unwrap();
        app.poll_events(&egui::Context::default());
        assert!(!app.player.active);
        assert!(!output.exists());
        assert!(app.error.is_none());
        assert!(app.status.starts_with("已取消"));
    }
    #[test]
    fn successful_atomic_export_wins_a_late_cancel_race() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = loaded_app(dir.path());
        let output = dir.path().join("finished.wav");
        std::fs::write(&output, b"completed").unwrap();
        app.job = Some(ActiveJob {
            id: 9,
            cancel: Arc::new(AtomicBool::new(true)),
            thread: thread::spawn(|| {}),
        });
        app.tx
            .send(Event::Done(
                9,
                JobKind::Audio,
                Ok(JobOutput::Export(output.clone())),
            ))
            .unwrap();
        app.poll_events(&egui::Context::default());
        assert_eq!(app.last_output, Some(output));
        assert!(app.status.starts_with("导出完成"));
    }
    #[test]
    fn changed_midi_requires_reload_and_new_source_folder_discards_stale_excerpts() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = loaded_app(dir.path());
        assert!(app.selected_data().is_ok());
        std::fs::write(
            app.loaded_path.as_ref().unwrap(),
            b"different and longer MIDI fixture",
        )
        .unwrap();
        assert!(app.selected_data().is_err());
        app.options.materials.insert(
            60,
            MaterialChoice {
                start: 1.,
                end: Some(3.),
                random: true,
            },
        );
        app.set_video_dir("different-folder".into());
        assert!(app.options.materials.is_empty());
        assert!(app.infos.is_empty());
    }
    #[test]
    fn headless_layout_keeps_actions_visible_and_paths_inside_small_window() {
        let directory = tempfile::tempdir().unwrap();
        let context = egui::Context::default();
        install_fonts(&context);
        let mut app = loaded_app(directory.path());
        app.video_dir = "very-long-material-directory/".repeat(12);
        app.output_path = format!("{}/finished.mp4", "very-long-output-directory".repeat(12));
        app.infos.insert(
            60,
            MediaInfo {
                path: "C4.wav".into(),
                duration: 5.,
                has_video: false,
                has_audio: true,
            },
        );
        app.choose_material(60).unwrap();
        app.error = Some("missing file / long diagnostic line\n".repeat(200));
        for (width, height) in [(880.0_f32, 680.0_f32), (1180.0_f32, 860.0_f32)] {
            for page in [Workspace::Input, Workspace::Materials, Workspace::Output] {
                app.workspace = page;
                let mut footer = egui::Rect::NOTHING;
                let mut content = egui::Rect::NOTHING;
                for _ in 0..3 {
                    let input = egui::RawInput {
                        screen_rect: Some(egui::Rect::from_min_size(
                            egui::Pos2::ZERO,
                            egui::vec2(width, height),
                        )),
                        ..Default::default()
                    };
                    let _ = context.run(input, |ctx| {
                        (footer, content) = app.show_workspace(ctx);
                    });
                }
                assert!(
                    footer.bottom() <= height + 1.0 && footer.top() >= height - 260.,
                    "footer {footer:?} in {width}x{height} {page:?}"
                );
                assert!(
                    footer.right() <= width + 1.0,
                    "footer {footer:?} in {width}x{height} {page:?}"
                );
                assert!(
                    content.right() <= width + 1.0,
                    "content {content:?} in {width}x{height} {page:?}"
                );
            }
        }
        assert!(!app.busy());
        assert!(!app.player.active);
    }
}
