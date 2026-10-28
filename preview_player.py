"""Non-blocking playback of rendered PCM previews, with an explicit stop."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import wave

_WINDOWS = sys.platform == "win32"
if _WINDOWS:
    import winsound as _winsound
else:
    _winsound = None


class PreviewPlaybackError(ValueError):
    """A preview cannot be played by this machine."""


class PreviewPlayer:
    """Play an existing WAV; creating a player never starts audio."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._deadline = 0.0
        self._duration = 0.0
        self._windows_active = False

    @property
    def duration(self) -> float:
        return self._duration

    def play(self, path: str | Path) -> None:
        self.stop()
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise PreviewPlaybackError(f"试听文件不存在：{source}")
        try:
            with wave.open(str(source), "rb") as audio:
                if audio.getcomptype() != "NONE" or audio.getframerate() <= 0:
                    raise ValueError("not PCM WAV")
                duration = audio.getnframes() / audio.getframerate()
        except (OSError, EOFError, ValueError, wave.Error) as error:
            raise PreviewPlaybackError("试听文件不是可播放的 PCM WAV 音频。") from error
        if duration <= 0:
            raise PreviewPlaybackError("试听文件没有声音时长。")
        with self._lock:
            self._duration = duration
            if _WINDOWS:
                try:
                    flags = _winsound.SND_FILENAME | _winsound.SND_ASYNC | _winsound.SND_NODEFAULT
                    _winsound.PlaySound(str(source), flags)
                except (OSError, RuntimeError) as error:
                    raise PreviewPlaybackError(f"无法播放试听，请检查声音输出设备：{error}") from error
                self._windows_active = True
                self._deadline = time.monotonic() + duration
                return
            if shutil.which("ffplay"):
                command = [shutil.which("ffplay"), "-nodisp", "-autoexit", "-loglevel", "error", str(source)]
            elif sys.platform == "darwin" and shutil.which("afplay"):
                command = [shutil.which("afplay"), str(source)]
            elif shutil.which("aplay"):
                command = [shutil.which("aplay"), "-q", str(source)]
            else:
                raise PreviewPlaybackError("系统没有可用的试听播放器。请安装 FFplay，或使用系统播放器打开生成的 WAV。")
            try:
                self._process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WINDOWS else 0,
                )
                try:
                    returncode = self._process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    return
                _, errors = self._process.communicate()
                self._process = None
                if returncode:
                    raise PreviewPlaybackError(
                        "试听播放器启动失败：" + errors.decode("utf-8", errors="replace")[-1000:].strip()
                    )
            except OSError as error:
                self._process = None
                raise PreviewPlaybackError(f"无法启动试听播放器：{error}") from error

    def stop(self) -> None:
        with self._lock:
            self._deadline = 0.0
            if self._windows_active:
                self._windows_active = False
                try:
                    _winsound.PlaySound(None, 0)
                except (OSError, RuntimeError):
                    # A disconnected output device must not prevent cleanup.
                    pass
            process, self._process = self._process, None
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=1)

    def is_playing(self) -> bool:
        with self._lock:
            if self._windows_active:
                if time.monotonic() < self._deadline:
                    return True
                self.stop()
                return False
            if self._process is not None:
                if self._process.poll() is None:
                    return True
                _, errors = self._process.communicate()
                code = self._process.returncode
                self._process = None
                if code:
                    raise PreviewPlaybackError(
                        "试听播放失败：" + errors.decode("utf-8", errors="replace")[-1000:].strip()
                    )
            return False
