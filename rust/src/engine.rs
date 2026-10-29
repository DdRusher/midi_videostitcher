use crate::model::{JobControl, MediaInfo, MidiData, RenderOptions, VisualMode};
use crate::{audio, media, timeline, video};
use anyhow::{bail, Context, Result};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

fn validate(options: &RenderOptions, extension: &str) -> Result<PathBuf> {
    if options.width < 16
        || options.width > 7680
        || options.width % 2 != 0
        || options.height < 16
        || options.height > 7680
        || options.height % 2 != 0
    {
        bail!("宽高必须是 16–7680 之间的偶数。");
    }
    if !(1..=120).contains(&options.fps) {
        bail!("帧率必须是 1–120 之间的整数。");
    }
    if options.workers > 32 {
        bail!("并行任务数必须为 0–32，0 表示自动。");
    }
    if !options.audio_start.is_finite() || options.audio_start < 0.0 {
        bail!("导入音频起点必须是有限的非负秒数。");
    }
    if options.output.as_os_str().is_empty() {
        bail!("请选择输出文件。");
    }
    if options
        .output
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        != extension
    {
        bail!("输出文件必须以 .{extension} 结尾。");
    }
    if options.output.is_dir() {
        bail!("保存位置需要包含文件名。");
    }
    let output = if options.output.is_absolute() {
        options.output.clone()
    } else {
        std::env::current_dir()?.join(&options.output)
    };
    if output.exists() && !options.overwrite {
        bail!("输出文件已存在，请更换文件名或勾选允许覆盖。");
    }
    Ok(output)
}

fn protect(output: &Path, inputs: impl Iterator<Item = PathBuf>) -> Result<()> {
    let normalized = output
        .canonicalize()
        .unwrap_or_else(|_| output.to_path_buf());
    for input in inputs {
        if normalized == input.canonicalize().unwrap_or(input.clone()) {
            bail!(
                "输出文件不能覆盖正在使用的素材或导入音频：{}",
                input.display()
            );
        }
    }
    Ok(())
}

pub fn inspect_materials(
    data: &MidiData,
    options: &RenderOptions,
    control: &JobControl,
) -> Result<BTreeMap<u8, MediaInfo>> {
    control.check()?;
    if let Some(source) = options.audio_path.as_ref() {
        if !source.is_file() {
            bail!("导入音频不存在：{}", source.display());
        }
        let ffmpeg = media::find_ffmpeg(options.ffmpeg.as_deref())?;
        let infos = media::probe_many(&ffmpeg, &[source.clone()], control)?;
        if !infos[source].has_audio {
            bail!("导入的文件没有音轨：{}", source.display());
        }
        if options.visuals == VisualMode::Labels
            || options
                .output
                .extension()
                .and_then(|extension| extension.to_str())
                .is_some_and(|extension| extension.eq_ignore_ascii_case("wav"))
        {
            return Ok(BTreeMap::new());
        }
    }
    media::inspect(data, options, control)
}

fn publish(source: &Path, output: &Path, overwrite: bool, control: &JobControl) -> Result<()> {
    control.check()?;
    if !source.is_file() || source.metadata()?.len() == 0 {
        bail!("编码器没有生成有效输出。");
    }
    let parent = output.parent().context("输出文件夹无效")?;
    let mut temporary = tempfile::Builder::new()
        .prefix(".midi-rust-publish-")
        .tempfile_in(parent)?;
    io::copy(&mut File::open(source)?, temporary.as_file_mut())?;
    temporary.as_file_mut().flush()?;
    control.check()?;
    if overwrite {
        temporary
            .persist(output)
            .map_err(|error| error.error)
            .context("无法替换输出文件；原输出仍保留")?;
    } else {
        temporary
            .persist_noclobber(output)
            .map_err(|error| error.error)
            .context("导出期间出现同名文件，已保留该文件；请换一个文件名")?;
    }
    Ok(())
}

fn export(
    data: &MidiData,
    options: &RenderOptions,
    control: &JobControl,
    audio_only: bool,
) -> Result<PathBuf> {
    control.check()?;
    let output = validate(options, if audio_only { "wav" } else { "mp4" })?;
    let ffmpeg = media::find_ffmpeg(options.ffmpeg.as_deref())?;
    let requires_assets =
        options.audio_path.is_none() || (!audio_only && options.visuals == VisualMode::Clips);
    let assets = if requires_assets {
        media::inspect(data, options, &control.scoped(0.0, 0.05, "正在检查素材…"))?
    } else {
        BTreeMap::new()
    };
    if audio_only && options.audio_path.is_none() && !assets.values().any(|info| info.has_audio) {
        bail!("所选素材没有可导出的音轨。");
    }
    if !audio_only && options.visuals == VisualMode::Clips {
        for (&pitch, info) in &assets {
            if !info.has_video {
                bail!(
                    "{} 没有视频轨道；请使用音名画面或提供视频素材。",
                    crate::model::note_name(pitch)
                );
            }
        }
    }
    if let Some(source) = options.audio_path.as_ref() {
        if !source.is_file() {
            bail!("导入音频不存在：{}", source.display());
        }
        let info = media::probe_many(&ffmpeg, &[source.clone()], control)?;
        if !info[source].has_audio {
            bail!("导入音频没有音轨：{}", source.display());
        }
    }
    protect(
        &output,
        assets
            .values()
            .map(|info| info.path.clone())
            .chain(options.audio_path.iter().cloned()),
    )?;
    let (planned, windows) = if requires_assets {
        timeline::plan_materials(data, options, &assets)?
    } else {
        (data.clone(), BTreeMap::new())
    };
    let schedule = timeline::build(&planned, options.fps, options.chord)?;
    let parent = output.parent().context("输出文件夹无效")?;
    fs::create_dir_all(parent)?;
    let temporary = tempfile::Builder::new()
        .prefix(".midi-rust-job-")
        .tempdir_in(parent)?;
    let work = temporary.path();
    let mix_control = control.scoped(0.05, if audio_only { 0.96 } else { 0.35 }, "正在生成音频…");
    let audio = audio::render_mix(
        &schedule,
        &assets,
        &windows,
        options,
        work,
        &ffmpeg,
        &mix_control,
    )?;
    let result = if audio_only {
        audio
    } else {
        video::render(
            &schedule,
            &assets,
            &windows,
            options,
            &ffmpeg,
            work,
            &audio,
            &control.scoped(0.35, 0.98, "正在生成画面…"),
        )?
    };
    publish(&result, &output, options.overwrite, control)?;
    control.report(1.0, format!("已保存：{}", output.display()));
    Ok(output)
}

pub fn render(data: &MidiData, options: &RenderOptions, control: &JobControl) -> Result<PathBuf> {
    export(data, options, control, false)
}
pub fn render_audio(
    data: &MidiData,
    options: &RenderOptions,
    control: &JobControl,
) -> Result<PathBuf> {
    export(data, options, control, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validation_preserves_old_output() {
        let directory = tempfile::tempdir().unwrap();
        let output = directory.path().join("old.mp4");
        fs::write(&output, b"old").unwrap();
        let options = RenderOptions {
            output: output.clone(),
            ..Default::default()
        };
        assert!(validate(&options, "mp4").is_err());
        assert_eq!(fs::read(output).unwrap(), b"old");
    }
    #[test]
    fn cancellation_and_no_clobber_protect_outputs() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("new.mp4");
        let output = directory.path().join("old.mp4");
        fs::write(&source, b"new").unwrap();
        fs::write(&output, b"old").unwrap();
        assert!(publish(&source, &output, false, &JobControl::default()).is_err());
        assert_eq!(fs::read(&output).unwrap(), b"old");
        let control = JobControl::default();
        control
            .cancel
            .store(true, std::sync::atomic::Ordering::Relaxed);
        assert!(publish(&source, &output, true, &control).is_err());
        assert_eq!(fs::read(output).unwrap(), b"old");
    }
}
