use std::collections::HashSet;
use std::ffi::{OsStr, OsString};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, OnceLock,
};
use std::thread;
use std::time::Duration;

use crate::model::JobControl;
use anyhow::{bail, Context, Result};

#[derive(Debug)]
pub struct ProcessOutput {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

pub fn args(values: &[&str]) -> Vec<OsString> {
    values.iter().map(OsString::from).collect()
}
pub fn ffmpeg_args() -> Vec<OsString> {
    args(&[
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
    ])
}

fn command(
    program: &Path,
    arguments: &[OsString],
    cwd: Option<&Path>,
    pipe_input: bool,
) -> Command {
    let mut command = Command::new(program);
    command
        .args(arguments)
        .stdin(if pipe_input {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(directory) = cwd {
        command.current_dir(directory);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
}

fn drain<R: Read>(reader: R, limit: usize) -> Vec<u8> {
    let mut result = Vec::new();
    let mut reader = BufReader::new(reader);
    let mut buffer = [0_u8; 8192];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(count) => {
                result.extend_from_slice(&buffer[..count]);
                if result.len() > limit {
                    result.drain(..result.len() - limit);
                }
            }
        }
    }
    result
}

fn read_stdout<R: Read>(reader: R, control: JobControl, seconds: f64) -> Vec<u8> {
    let mut result = Vec::new();
    let mut reader = BufReader::new(reader);
    let mut line = Vec::new();
    loop {
        line.clear();
        match reader.read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                if seconds > 0.0 {
                    if let Some(value) = String::from_utf8_lossy(&line)
                        .trim()
                        .strip_prefix("out_time_us=")
                    {
                        if let Ok(microseconds) = value.parse::<u64>() {
                            control.report(
                                (microseconds as f64 / 1_000_000.0 / seconds).min(0.99),
                                "正在编码…",
                            );
                        }
                    }
                }
                result.extend_from_slice(&line);
                if result.len() > 4 * 1024 * 1024 {
                    result.drain(..result.len() - 4 * 1024 * 1024);
                }
            }
        }
    }
    result
}

fn terminate_child(child: &mut Child) {
    if child.try_wait().ok().flatten().is_none() {
        #[cfg(windows)]
        {
            // Kill descendants too when PATH resolves to a launcher/shim.
            use std::os::windows::process::CommandExt;
            let _ = Command::new("taskkill.exe")
                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                .creation_flags(0x0800_0000)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        let _ = child.kill();
    }
    let _ = child.wait();
}
fn wait_child(mut child: Child, control: &JobControl, abort: &AtomicBool) -> Result<i32> {
    loop {
        if control.cancel.load(Ordering::Relaxed) || abort.load(Ordering::Relaxed) {
            terminate_child(&mut child);
            control.check()?;
            bail!("编码任务已停止。");
        }
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status.code().unwrap_or(-1)),
            Ok(None) => thread::sleep(Duration::from_millis(15)),
            Err(error) => {
                terminate_child(&mut child);
                return Err(error.into());
            }
        }
    }
}

pub fn run(
    program: &Path,
    arguments: &[OsString],
    cwd: Option<&Path>,
    control: &JobControl,
    expected_seconds: f64,
) -> Result<ProcessOutput> {
    control.check()?;
    let mut child = command(program, arguments, cwd, false)
        .spawn()
        .with_context(|| format!("无法启动 {}", program.display()))?;
    let stdout = child.stdout.take().context("无法读取编码输出")?;
    let stderr = child.stderr.take().context("无法读取编码错误")?;
    let progress = control.clone();
    let output = thread::spawn(move || read_stdout(stdout, progress, expected_seconds));
    let errors = thread::spawn(move || drain(stderr, 512 * 1024));
    let result = wait_child(child, control, &AtomicBool::new(false));
    let stdout = output.join().unwrap_or_default();
    let stderr = errors.join().unwrap_or_default();
    Ok(ProcessOutput {
        code: result?,
        stdout,
        stderr,
    })
}

static MODERN_FILTER_FILE: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();

pub fn encode(
    program: &Path,
    arguments: &[OsString],
    cwd: Option<&Path>,
    control: &JobControl,
    expected_seconds: f64,
) -> Result<()> {
    let key = program
        .canonicalize()
        .unwrap_or_else(|_| program.to_path_buf());
    let modern = MODERN_FILTER_FILE
        .get_or_init(Mutex::default)
        .lock()
        .unwrap()
        .contains(&key);
    let mut selected = arguments.to_vec();
    if modern {
        for argument in &mut selected {
            if argument == OsStr::new("-filter_complex_script") {
                *argument = "-/filter_complex".into();
            }
        }
    }
    let mut output = run(program, &selected, cwd, control, expected_seconds)?;
    if output.code != 0
        && selected.iter().any(|item| item == "-filter_complex_script")
        && String::from_utf8_lossy(&output.stderr).lines().any(|line| {
            let line = line.trim();
            line == "Unrecognized option 'filter_complex_script'."
                || line == "Unrecognized option 'filter_complex_script'"
        })
    {
        control.check()?;
        MODERN_FILTER_FILE
            .get()
            .unwrap()
            .lock()
            .unwrap()
            .insert(key);
        for argument in &mut selected {
            if argument == OsStr::new("-filter_complex_script") {
                *argument = "-/filter_complex".into();
            }
        }
        output = run(program, &selected, cwd, control, expected_seconds)?;
    }
    if output.code != 0 {
        bail!(
            "FFmpeg 导出失败：\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    control.report(1.0, "编码完成");
    Ok(())
}

/// A watcher owns the child while the caller streams frames. Cancellation kills
/// a blocked encoder and releases its stdin, so no GUI worker can hang in write.
pub fn encode_frames<F>(
    program: &Path,
    arguments: &[OsString],
    cwd: Option<&Path>,
    control: &JobControl,
    expected_seconds: f64,
    write_frames: F,
) -> Result<()>
where
    F: FnOnce(&mut dyn Write) -> Result<()>,
{
    control.check()?;
    let mut child = command(program, arguments, cwd, true)
        .spawn()
        .with_context(|| format!("无法启动 {}", program.display()))?;
    let mut stdin = child.stdin.take().context("无法写入视频帧")?;
    let stdout = child.stdout.take().context("无法读取编码输出")?;
    let stderr = child.stderr.take().context("无法读取编码错误")?;
    let progress = control.clone();
    let output = thread::spawn(move || read_stdout(stdout, progress, expected_seconds));
    let errors = thread::spawn(move || drain(stderr, 512 * 1024));
    let abort = Arc::new(AtomicBool::new(false));
    let watcher_abort = abort.clone();
    let watcher_control = control.clone();
    let watcher = thread::spawn(move || wait_child(child, &watcher_control, &watcher_abort));
    let written = write_frames(&mut stdin);
    drop(stdin);
    if written.is_err() {
        abort.store(true, Ordering::Relaxed);
    }
    let status = watcher
        .join()
        .map_err(|_| anyhow::anyhow!("编码进程监控失败"))?;
    let _stdout = output.join().unwrap_or_default();
    let stderr = errors.join().unwrap_or_default();
    control.check()?;
    if let Err(error) = written {
        if !stderr.is_empty() {
            bail!(
                "视频编码中断：{error}\n{}",
                String::from_utf8_lossy(&stderr)
            );
        }
        return Err(error);
    }
    let code = status?;
    if code != 0 {
        bail!("FFmpeg 导出失败：\n{}", String::from_utf8_lossy(&stderr));
    }
    control.report(1.0, "编码完成");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cancellation_prevents_spawn() {
        let control = JobControl::default();
        control.cancel.store(true, Ordering::Relaxed);
        assert!(
            run(Path::new("missing-executable"), &[], None, &control, 0.0)
                .unwrap_err()
                .to_string()
                .contains("取消")
        );
    }
    #[test]
    fn output_drain_is_bounded() {
        assert_eq!(drain(&b"123456789"[..], 4), b"6789");
    }
}
