"""Render a MIDI timeline with selected media windows and bounded NumPy mixing.

MIDI times are quantized against one absolute frame clock.  Concurrent notes
remain independent voices, with a dynamic video grid and one continuous mix.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, as_completed
import heapq
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import tempfile
import threading
from typing import TYPE_CHECKING, Callable

from material_selection import MaterialSelection, material_windows, plan_material_offsets

if TYPE_CHECKING:
    from notes import MidiData


class RenderError(ValueError):
    """An actionable validation or encoding error."""


class RenderCancelled(Exception):
    """The user cancelled an export; an existing output is left untouched."""


@dataclass(frozen=True)
class RenderOptions:
    video_dir: str | Path
    output_path: str | Path
    width: int = 1280
    height: int = 720
    fps: int = 30
    chord_policy: str = "all"
    audio_path: str | Path | None = None
    use_clip_audio: bool = True
    overwrite: bool = False
    ffmpeg_path: str | None = None
    material_selections: dict[int, MaterialSelection] | None = None
    random_seed: int = 0
    respect_velocity: bool = True
    video_mode: str = "clips"
    audio_start: float = 0.0
    render_workers: int = 0


@dataclass(frozen=True)
class TimelineSegment:
    start_frame: int
    end_frame: int
    pitch: int | None
    source_offset_frames: int = 0
    source_offset: float = 0.0

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class TimelineVoice:
    note_id: int
    pitch: int
    source_offset: float
    velocity: int = 127


@dataclass(frozen=True)
class PolyphonicSegment:
    start_frame: int
    end_frame: int
    voices: tuple[TimelineVoice, ...]

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class _QuantizedNote:
    note_id: int
    pitch: int
    first: int
    last: int
    start: float
    source_offset: float
    velocity: int = 127


@dataclass(frozen=True)
class _AudioInterval:
    note_id: int
    pitch: int
    first: int
    last: int
    source_offset: float
    velocity: int = 127


_AUDIO_RATE = 48000


_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
_AUDIO_EXTENSIONS = frozenset({".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aac", ".wma"})
_SHARPS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLATS = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")


def _pitch_name(pitch: int, *, flat: bool = False) -> str:
    return f"{(_FLATS if flat else _SHARPS)[pitch % 12]}{pitch // 12 - 1}"


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RenderCancelled("已取消导出，原有输出文件未修改。")


def _notify(callback: Callable[[float, str], None] | None, value: float, message: str) -> None:
    if callback is not None:
        callback(min(1.0, max(0.0, value)), message)


def find_ffmpeg(explicit: str | None = None) -> str:
    """Find an explicitly selected FFmpeg, PATH FFmpeg, or bundled binary."""
    if explicit:
        candidate = shutil.which(str(explicit))
        if candidate is None:
            path = Path(explicit).expanduser()
            if path.is_file():
                candidate = str(path.resolve())
        if candidate is None:
            raise RenderError(f"找不到指定的 FFmpeg：{explicit}")
        return str(Path(candidate).resolve())
    candidate = shutil.which("ffmpeg")
    if candidate:
        return str(Path(candidate).resolve())
    try:
        import imageio_ffmpeg

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and Path(candidate).is_file():
            return candidate
    except (ImportError, RuntimeError, OSError):
        pass
    raise RenderError("找不到 FFmpeg。请先安装项目依赖，或安装 FFmpeg 并加入 PATH。")


def validate_assets(data: MidiData, video_dir: str | Path, *, allow_audio: bool = False) -> dict[int, Path]:
    """Map pitches to files, rejecting missing notes and ambiguous filenames.

    A note can be named C#4, Db4, or 61, with a supported video extension.
    Matching is case insensitive.  Multiple aliases for one pitch are rejected
    so changing file-system enumeration order cannot change the rendered song.
    """
    directory = Path(video_dir).expanduser()
    if not directory.is_dir():
        raise RenderError(f"视频素材文件夹不存在：{directory}")
    extensions = _EXTENSIONS | _AUDIO_EXTENSIONS if allow_audio else _EXTENSIONS
    entries = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in extensions),
        key=lambda p: (p.name.casefold(), p.name),
    )
    pitches = sorted({int(note.pitch) for note in data.notes})
    if not pitches:
        raise RenderError("所选轨道或通道没有可导出的音符。")
    result: dict[int, Path] = {}
    missing: list[str] = []
    for pitch in pitches:
        if not 0 <= pitch <= 127:
            raise RenderError(f"MIDI 音高超出范围：{pitch}")
        aliases = {str(pitch), _pitch_name(pitch).casefold(), _pitch_name(pitch, flat=True).casefold()}
        matches = [p for p in entries if p.stem.replace("♯", "#").replace("♭", "b").casefold() in aliases]
        if len(matches) > 1:
            names = "、".join(p.name for p in matches)
            raise RenderError(f"音符 {_pitch_name(pitch)} 对应多个素材：{names}。请只保留其中一个。")
        if not matches:
            missing.append(f"{_pitch_name(pitch)}（或 {pitch}）")
        else:
            result[pitch] = matches[0].resolve()
    if missing:
        raise RenderError("缺少以下音符的视频素材：" + "、".join(missing) + "。例如 C4.mp4 或 60.mp4。")
    return result


def _note_frame_bounds(start: float, end: float, total: int, fps: int) -> tuple[int, int]:
    """Use the same absolute-clock quantization for rendering and warnings."""
    first = max(0, min(total, math.floor(start * fps + 0.5)))
    last = max(0, min(total, math.floor(end * fps + 0.5)))
    return first, last


def _quantize_notes(data: MidiData, fps: int) -> tuple[int, dict[int, _QuantizedNote], int]:
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 120:
        raise RenderError("帧率必须是 1 到 120 之间的整数。")
    duration = float(data.duration)
    if not math.isfinite(duration) or duration <= 0:
        raise RenderError("MIDI 时长必须大于零。")
    if not data.notes:
        raise RenderError("所选轨道或通道没有可导出的音符。")
    total = max(1, math.ceil(duration * fps - 1e-9))
    notes: dict[int, _QuantizedNote] = {}
    skipped = 0
    for index, note in enumerate(data.notes):
        start, end = float(note.start), float(note.end)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise RenderError("MIDI 中包含无效的音符起止时间。")
        pitch = int(note.pitch)
        if not 0 <= pitch <= 127:
            raise RenderError(f"MIDI 音高超出范围：{pitch}")
        velocity = getattr(note, "velocity", 127)
        if isinstance(velocity, bool) or not isinstance(velocity, int) or not 1 <= velocity <= 127:
            raise RenderError("音符力度必须是 1 到 127 之间的整数。")
        first, last = _note_frame_bounds(start, end, total, fps)
        source_offset = float(getattr(note, "source_offset", 0.0))
        if not math.isfinite(source_offset) or source_offset < 0:
            raise RenderError("素材起始偏移必须是有限的非负秒数。")
        if last <= first:
            skipped += 1
            continue
        notes[index] = _QuantizedNote(index, pitch, first, last, start, source_offset, velocity)
    if not notes:
        raise RenderError(f"全部 {len(data.notes)} 个音符在当前帧率下都不足一帧，无法生成可见音符画面；请提高帧率。")
    return total, notes, skipped


def _timeline(data: MidiData, fps: int, chord_policy: str):
    if chord_policy not in {"all", "highest", "lowest", "latest"}:
        raise RenderError("和弦处理方式必须是 all、highest、lowest 或 latest。")
    total, notes, skipped = _quantize_notes(data, fps)
    starts: dict[int, list[int]] = {}
    ends: dict[int, list[int]] = {}
    for note in notes.values():
        starts.setdefault(note.first, []).append(note.note_id)
        ends.setdefault(note.last, []).append(note.note_id)
    boundaries = sorted({0, total, *starts, *ends})
    active: set[int] = set()
    heap: list[tuple[float, float, int, int]] = []
    spans: list[tuple[int, int, tuple[int, ...]]] = []
    for first, last in zip(boundaries, boundaries[1:]):
        for index in ends.get(first, ()):
            active.discard(index)
        for index in starts.get(first, ()):
            active.add(index)
            pitch, start = notes[index].pitch, notes[index].start
            if chord_policy == "highest":
                key = (-pitch, -start, -index, index)
            elif chord_policy == "lowest":
                key = (pitch, -start, -index, index)
            elif chord_policy == "latest":
                key = (-start, -pitch, -index, index)
            if chord_policy != "all":
                heapq.heappush(heap, key)
        if chord_policy == "all":
            selected = tuple(sorted(active, key=lambda index: (notes[index].pitch, notes[index].start, index)))
        else:
            while heap and heap[0][3] not in active:
                heapq.heappop(heap)
            selected = (heap[0][3],) if heap else ()
        if spans and selected == spans[-1][2] and spans[-1][1] == first:
            spans[-1] = (spans[-1][0], last, selected)
        else:
            spans.append((first, last, selected))
    return spans, notes, skipped


def build_timeline(data: MidiData, fps: int = 30, chord_policy: str = "highest") -> tuple[TimelineSegment, ...]:
    """Backward-compatible single-voice timeline; default remains highest."""
    if chord_policy == "all":
        raise RenderError("多声部时间线请使用 build_polyphonic_timeline。")
    spans, notes, _ = _timeline(data, fps, chord_policy)
    return tuple(
        TimelineSegment(first, last, notes[voices[0]].pitch, first - notes[voices[0]].first, notes[voices[0]].source_offset)
        if voices else TimelineSegment(first, last, None)
        for first, last, voices in spans
    )


def build_polyphonic_timeline(data: MidiData, fps: int = 30, chord_policy: str = "all") -> tuple[PolyphonicSegment, ...]:
    """Keep every note's identity, including overlapping repetitions of a pitch."""
    spans, notes, _ = _timeline(data, fps, chord_policy)
    return tuple(PolyphonicSegment(first, last, tuple(
        TimelineVoice(index, notes[index].pitch, notes[index].source_offset + (first - notes[index].first) / fps, notes[index].velocity)
        for index in voices
    )) for first, last, voices in spans)


def _run_process(
    command: list[str],
    cancel_event: threading.Event | None,
    progress: Callable[[float], None] | None = None,
    expected_seconds: float = 0.0,
    cwd: str | Path | None = None,
) -> tuple[int, bytes, bytes]:
    _check_cancel(cancel_event)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, cwd=cwd)
    except OSError as exc:
        raise RenderError(f"无法启动 FFmpeg：{exc}") from exc
    try:
        while True:
            _check_cancel(cancel_event)
            try:
                stdout, stderr = process.communicate(timeout=0.15)
                if progress is not None:
                    progress(1.0)
                return process.returncode, stdout, stderr
            except subprocess.TimeoutExpired as exc:
                if progress is not None and expected_seconds > 0 and exc.output:
                    matches = re.findall(rb"out_time_us=(\d+)", exc.output)
                    if matches:
                        progress(min(0.99, int(matches[-1]) / 1_000_000 / expected_seconds))
    except BaseException:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise


_FILTER_SCRIPT_OPTIONS: dict[str, str] = {}


def _encode(command: list[str], cancel_event: threading.Event | None, progress=None, expected_seconds=0.0, *, cwd=None) -> None:
    legacy_option = "-filter_complex_script"
    modern_option = "-/filter_complex"
    executable = str(Path(command[0]).expanduser().resolve())
    selected_option = _FILTER_SCRIPT_OPTIONS.get(executable, legacy_option)
    selected_command = [selected_option if item == legacy_option else item for item in command]

    def report(fraction):
        # A rejected command must not report completion before the retry starts.
        if progress is not None and fraction < 1.0:
            progress(fraction)

    callback = report if progress is not None else None
    code, _, stderr = _run_process(selected_command, cancel_event, callback, expected_seconds, cwd)
    if (code and legacy_option in selected_command and
            re.search(rb"(?m)^\s*Unrecognized option ['\"]filter_complex_script['\"]\.?\s*$", stderr)):
        # FFmpeg 9 removed the old option; older releases still need it.
        # Retry only this parser rejection, never an actual encoding failure.
        _check_cancel(cancel_event)
        _FILTER_SCRIPT_OPTIONS[executable] = modern_option
        selected_command = [modern_option if item == legacy_option else item for item in selected_command]
        code, _, stderr = _run_process(selected_command, cancel_event, callback, expected_seconds, cwd)
    if code:
        detail = stderr.decode("utf-8", errors="replace")[-5000:].strip()
        raise RenderError(f"FFmpeg 导出失败：\n{detail}")
    if progress is not None:
        progress(1.0)


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    has_video: bool
    has_audio: bool


_MEDIA_INFO_CACHE: dict[tuple, MediaInfo] = {}
_MEDIA_CACHE_LOCK = threading.Lock()


def _media_key(ffmpeg: str, path: Path) -> tuple:
    stat = path.stat()
    return (str(Path(ffmpeg).resolve()), str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _precise_duration(path: Path, text: str, fallback: float, cancel_event) -> float:
    """Read precise container clocks instead of FFmpeg's two-decimal display."""
    tags = re.findall(r"DURATION\s*:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if tags:
        return max(int(hours) * 3600 + int(minutes) * 60 + float(seconds) for hours, minutes, seconds in tags)
    if path.suffix.lower() == ".wav":
        import wave
        try:
            with wave.open(str(path), "rb") as source:
                return source.getnframes() / source.getframerate()
        except (OSError, EOFError, wave.Error):
            pass
    if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".m4a"}:
        try:
            with path.open("rb") as source:
                size = path.stat().st_size

                def boxes(first, last):
                    position = first
                    while position + 8 <= last:
                        source.seek(position)
                        length, kind = struct.unpack(">I4s", source.read(8))
                        header = 8
                        if length == 1:
                            length = struct.unpack(">Q", source.read(8))[0]
                            header = 16
                        elif length == 0:
                            length = last - position
                        if length < header or position + length > last:
                            break
                        if kind == b"moov":
                            result = boxes(position + header, position + length)
                            if result is not None:
                                return result
                        elif kind == b"mvhd":
                            payload = source.read(min(32, length - header))
                            if payload[0] == 0 and len(payload) >= 20:
                                clock, duration = struct.unpack(">II", payload[12:20])
                            elif payload[0] == 1 and len(payload) >= 32:
                                clock, duration = struct.unpack(">IQ", payload[20:32])
                            else:
                                break
                            if clock and duration:
                                return duration / clock
                        position += length
                    return None

                duration = boxes(0, size)
                if duration is not None:
                    return duration
        except (OSError, EOFError, IndexError, struct.error):
            pass
    ffprobe = shutil.which("ffprobe")
    if ffprobe is not None:
        code, stdout, _ = _run_process([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], cancel_event)
        if not code:
            try:
                duration = float(json.loads(stdout)["format"]["duration"])
                if math.isfinite(duration) and duration > 0:
                    return duration
            except (ValueError, KeyError, TypeError):
                pass
    return fallback


def _parse_media_info(path: Path, text: str, cancel_event=None) -> MediaInfo:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = (int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) if match else 0.0
    has_video = bool(re.search(r"Stream #.*?: Video:", text))
    has_audio = bool(re.search(r"Stream #.*?: Audio:", text))
    if has_video or has_audio:
        duration = _precise_duration(path, text, duration, cancel_event)
    return MediaInfo(path, duration, has_video, has_audio)


def _probe_batch(ffmpeg: str, paths, cancel_event) -> None:
    """Probe up to sixteen distinct inputs per process, cached by file version."""
    pending = []
    for path in dict.fromkeys(paths):
        _check_cancel(cancel_event)
        key = _media_key(ffmpeg, path)
        with _MEDIA_CACHE_LOCK:
            cached = _MEDIA_INFO_CACHE.get(key)
        if cached is None and path.stat().st_size:
            pending.append((path, key))
    for first in range(0, len(pending), 16):
        batch = pending[first:first + 16]
        command = [ffmpeg, "-hide_banner", "-nostdin"]
        for path, _ in batch:
            command += ["-i", str(path)]
        _, _, stderr = _run_process(command, cancel_event)
        text = stderr.decode("utf-8", errors="replace")
        sections = re.split(r"(?m)^Input #(\d+),", text)
        parsed = {int(sections[index]): sections[index + 1] for index in range(1, len(sections) - 1, 2)}
        for index, (path, key) in enumerate(batch):
            info = _parse_media_info(path, parsed.get(index, ""), cancel_event)
            if info.has_video or info.has_audio:
                with _MEDIA_CACHE_LOCK:
                    _MEDIA_INFO_CACHE[key] = info


def _probe_info(ffmpeg: str, path: Path, cancel_event) -> MediaInfo:
    _check_cancel(cancel_event)
    key = _media_key(ffmpeg, path)
    with _MEDIA_CACHE_LOCK:
        cached = _MEDIA_INFO_CACHE.get(key)
    if cached is not None:
        return cached
    _, _, stderr = _run_process([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], cancel_event)
    info = _parse_media_info(path, stderr.decode("utf-8", errors="replace"), cancel_event)
    with _MEDIA_CACHE_LOCK:
        _MEDIA_INFO_CACHE[key] = info
    return info


def _probe(ffmpeg: str, path: Path, cancel_event: threading.Event | None) -> tuple[bool, bool]:
    info = _probe_info(ffmpeg, path, cancel_event)
    return info.has_video, info.has_audio


def inspect_materials(data: MidiData, video_dir: str | Path, ffmpeg_path: str | None = None,
                      cancel_event: threading.Event | None = None) -> dict[int, MediaInfo]:
    """Return durations and stream availability for named video or audio assets."""
    _check_cancel(cancel_event)
    assets = validate_assets(data, video_dir, allow_audio=True)
    ffmpeg = find_ffmpeg(ffmpeg_path)
    _probe_batch(ffmpeg, assets.values(), cancel_event)
    result = {}
    for pitch, path in assets.items():
        info = _probe_info(ffmpeg, path, cancel_event)
        if not (info.has_video or info.has_audio) or info.duration <= 0:
            raise RenderError(f"无法读取素材时长或音视频轨道：{path.name}")
        result[pitch] = info
    return result


def _validate_options(options: RenderOptions, output_path: str | Path | None = None, extension: str = ".mp4") -> Path:
    for value, label in ((options.width, "宽度"), (options.height, "高度")):
        if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 7680 or value % 2:
            raise RenderError(f"视频{label}必须是 16 到 7680 之间的偶数。")
    if options.video_mode not in {"clips", "labels"}:
        raise RenderError("画面模式必须是 clips（素材视频）或 labels（音名频率）。")
    if isinstance(options.random_seed, bool) or not isinstance(options.random_seed, int):
        raise RenderError("随机种子必须是整数。")
    if isinstance(options.render_workers, bool) or not isinstance(options.render_workers, int) or not 0 <= options.render_workers <= 32:
        raise RenderError("并行任务数必须是 0 到 32 之间的整数，0 表示自动。")
    if not isinstance(options.respect_velocity, bool):
        raise RenderError("力度控制开关必须是真或假。")
    if isinstance(options.audio_start, bool) or not math.isfinite(float(options.audio_start)) or float(options.audio_start) < 0:
        raise RenderError("导入音频起点必须是有限的非负秒数。")
    selected_output = options.output_path if output_path is None else output_path
    if not str(selected_output).strip():
        raise RenderError("请选择输出文件。")
    output = Path(selected_output).expanduser().resolve()
    if output.suffix.lower() != extension:
        raise RenderError(f"输出文件请使用 {extension} 扩展名。")
    if output.is_dir():
        raise RenderError(f"输出路径指向文件夹，请选择一个 {extension} 文件名。")
    return output


@dataclass(frozen=True)
class _RenderContext:
    output: Path
    segments: tuple[PolyphonicSegment, ...]
    assets: dict[int, Path]
    audio_by_pitch: dict[int, bool]
    ffmpeg: str
    soundtrack: Path | None
    durations: dict[int, float]
    windows: dict[int, tuple[float, float]]
    data: MidiData


def _prepare(
    data: MidiData,
    options: RenderOptions,
    on_progress,
    cancel_event,
    *,
    output_path: str | Path | None = None,
    extension: str = ".mp4",
) -> _RenderContext:
    _check_cancel(cancel_event)
    output = _validate_options(options, output_path, extension)
    segments = build_polyphonic_timeline(data, options.fps, options.chord_policy)
    _, _, skipped = _quantize_notes(data, options.fps)
    if skipped:
        _notify(on_progress, 0.0, f"有 {skipped} 个音符短于当前帧率可表示的时长，未显示；可提高帧率。")
    soundtrack = Path(options.audio_path).expanduser().resolve() if options.audio_path else None
    if soundtrack is not None and not soundtrack.is_file():
        raise RenderError(f"导入音频文件不存在：{soundtrack}")
    audio_only = extension == ".wav"
    requires_assets = soundtrack is None or (not audio_only and options.video_mode == "clips")
    assets = validate_assets(data, options.video_dir, allow_audio=audio_only or options.video_mode == "labels") if requires_assets else {}
    protected = [*assets.values(), *([soundtrack] if soundtrack else [])]
    for path in protected:
        if output == path or (output.exists() and output.samefile(path)):
            raise RenderError("输出文件不能覆盖输入的视频素材或配乐，请换一个输出路径。")
    if output.exists() and not options.overwrite:
        raise RenderError(f"输出文件已经存在：{output}。请换一个文件名，或允许覆盖。")
    ffmpeg = find_ffmpeg(options.ffmpeg_path)
    _probe_batch(ffmpeg, assets.values(), cancel_event)
    audio_by_pitch, durations = {}, {}
    for pitch, source in assets.items():
        _check_cancel(cancel_event)
        _notify(on_progress, 0.0, f"检查素材：{source.name}")
        has_video, has_audio = _probe(ffmpeg, source, cancel_event)
        if not audio_only and options.video_mode == "clips" and not has_video:
            raise RenderError(f"素材没有可读取的视频画面：{source.name}")
        if not (has_video or has_audio):
            raise RenderError(f"素材没有可读取的音视频轨道：{source.name}")
        audio_by_pitch[pitch] = has_audio
        with _MEDIA_CACHE_LOCK:
            info = _MEDIA_INFO_CACHE.get(_media_key(ffmpeg, source))
        if info is not None and info.duration > 0:
            durations[pitch] = info.duration
        if not has_audio and options.use_clip_audio and soundtrack is None:
            _notify(on_progress, 0.0, f"素材 {source.name} 没有音轨，该音符将保持静音。")
    if soundtrack and not _probe(ffmpeg, soundtrack, cancel_event)[1]:
        raise RenderError(f"导入音频文件中没有可读取的音轨：{soundtrack.name}")
    selected_data = data
    windows = {pitch: (0.0, duration) for pitch, duration in durations.items()}
    if options.material_selections and assets:
        try:
            windows = material_windows(options.material_selections, durations)
            selected_data = plan_material_offsets(data, options.material_selections, durations, options.random_seed)
        except ValueError as error:
            raise RenderError(str(error)) from error
        segments = build_polyphonic_timeline(selected_data, options.fps, options.chord_policy)
    _check_cancel(cancel_event)
    return _RenderContext(output, segments, assets, audio_by_pitch, ffmpeg, soundtrack, durations, windows, selected_data)


def _sample_at_frame(frame: int, fps: int) -> int:
    return (frame * _AUDIO_RATE * 2 + fps) // (2 * fps)


def _command(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]


def _audio_intervals(segments: tuple[PolyphonicSegment, ...]) -> list[_AudioInterval]:
    """Join a held note across changing grids without resetting its sound."""
    intervals: list[_AudioInterval] = []
    last_by_id: dict[int, int] = {}
    for segment in segments:
        for voice in segment.voices:
            index = last_by_id.get(voice.note_id)
            if index is not None and intervals[index].last == segment.start_frame:
                intervals[index] = replace(intervals[index], last=segment.end_frame)
            else:
                last_by_id[voice.note_id] = len(intervals)
                intervals.append(_AudioInterval(voice.note_id, voice.pitch, segment.start_frame, segment.end_frame, voice.source_offset, voice.velocity))
    return sorted(intervals, key=lambda interval: (interval.first, interval.last, interval.note_id))


def _filter_script(path: Path, filters: list[str]) -> str:
    path.write_text(";\n".join(filters) + "\n", encoding="utf-8")
    return str(path)


def _write_silence(path: Path, sample_count: int, cancel_event, report) -> None:
    import wave

    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(_AUDIO_RATE)
        written = 0
        while written < sample_count:
            _check_cancel(cancel_event)
            chunk = min(16384, sample_count - written)
            output.writeframesraw(b"\0" * (chunk * 4))
            written += chunk
        report(1.0, "素材没有音轨，成片保留静音。")


def _decode_audio_materials(context, pitches, work, cancel_event, report):
    """Decode each source once, grouping inputs to avoid per-note processes."""
    import numpy as np

    paths = {}
    entries = sorted(pitches)
    for first in range(0, len(entries), 16):
        _check_cancel(cancel_event)
        batch = entries[first:first + 16]
        command = _command(context.ffmpeg)
        for pitch in batch:
            command += ["-i", str(context.assets[pitch])]
        for index, pitch in enumerate(batch):
            destination = work / f"source-audio-{pitch:03d}.f32"
            window = context.windows.get(pitch)
            filters = f"aresample={_AUDIO_RATE}:async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=stereo"
            if window is not None:
                start = round(window[0] * _AUDIO_RATE)
                end = round(window[1] * _AUDIO_RATE)
                filters += f",atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS,apad,atrim=end_sample={end-start}"
            filters += ",asetpts=PTS-STARTPTS"
            command += ["-map", f"{index}:a:0", "-vn", "-af", filters,
                        "-c:a", "pcm_f32le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-f", "f32le", str(destination)]
            paths[pitch] = destination
        _encode(command, cancel_event, expected_seconds=max((context.windows.get(pitch, (0, 1))[1] for pitch in batch), default=1))
        report(0.30 * min(1.0, (first + len(batch)) / len(entries)), f"已读取素材原声 {first + len(batch)}/{len(entries)}…")
    decoded = {}
    try:
        for pitch, path in paths.items():
            sample_count = path.stat().st_size // 8
            if sample_count <= 0:
                raise RenderError(f"所选素材片段没有可读取的音频样本：{context.assets[pitch].name}")
            decoded[pitch] = np.memmap(path, dtype="<f4", mode="r", shape=(sample_count, 2))
        return decoded
    except BaseException:
        for samples in decoded.values():
            samples._mmap.close()
        raise


def _render_audio(context, options, work, cancel_event, report, *, allow_silence: bool) -> Path:
    """Vectorized bounded mixing, decoding once per source rather than note."""
    total_samples = _sample_at_frame(context.segments[-1].end_frame, options.fps)
    destination = work / "mixed-original-audio.wav"
    if context.soundtrack is not None:
        first = round(float(options.audio_start) * _AUDIO_RATE)
        filters = (
            f"aresample={_AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=start_sample={first},asetpts=PTS-STARTPTS,apad,atrim=end_sample={total_samples},"
            f"alimiter=limit=0.95:level=false:latency=true,apad,atrim=end_sample={total_samples}"
        )
        command = _command(context.ffmpeg) + ["-i", str(context.soundtrack), "-map", "0:a:0", "-vn", "-af", filters, "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-rf64", "auto", str(destination)]
        _encode(command, cancel_event, lambda ratio: report(ratio, "正在截取导入音频…"), total_samples / _AUDIO_RATE)
        return destination
    intervals = [interval for interval in _audio_intervals(context.segments) if options.use_clip_audio and context.audio_by_pitch[interval.pitch]]
    if not intervals:
        if not allow_silence:
            if not options.use_clip_audio:
                raise RenderError("素材原声已关闭，无法生成有声试听。")
            raise RenderError("所选音符的视频素材没有可试听的音轨；成片仍可导出静音视频。")
        _write_silence(destination, total_samples, cancel_event, report)
        return destination
    import numpy as np

    decoded = _decode_audio_materials(context, {interval.pitch for interval in intervals}, work, cancel_event, report)
    mixed = None
    mixed_path = work / "mixed-voices.f32"
    try:
        mixed = np.memmap(mixed_path, dtype="<f4", mode="w+", shape=(total_samples, 2))
        for first in range(0, total_samples, 65536):
            _check_cancel(cancel_event)
            mixed[first:first + 65536] = 0
        for index, interval in enumerate(intervals):
            _check_cancel(cancel_event)
            source = decoded[interval.pitch]
            first = _sample_at_frame(interval.first, options.fps)
            last = _sample_at_frame(interval.last, options.fps)
            source_index = math.floor(interval.source_offset * _AUDIO_RATE + 0.5) % len(source)
            gain = interval.velocity / 127.0 if options.respect_velocity else 1.0
            target = first
            while target < last:
                _check_cancel(cancel_event)
                count = min(last - target, len(source) - source_index, 65536)
                mixed[target:target + count] += source[source_index:source_index + count] * gain
                target += count
                source_index = (source_index + count) % len(source)
            report(0.30 + 0.50 * (index + 1) / len(intervals), f"正在叠加音符原声 {index + 1}/{len(intervals)}…")
        mixed.flush()
    finally:
        if mixed is not None:
            mixed._mmap.close()
        for samples in decoded.values():
            samples._mmap.close()
    filters = f"alimiter=limit=0.95:level=false:latency=true:attack=5:release=50,apad,atrim=end_sample={total_samples}"
    command = _command(context.ffmpeg) + ["-f", "f32le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-i", str(mixed_path), "-af", filters,
               "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-rf64", "auto", str(destination)]
    _encode(command, cancel_event, lambda ratio: report(0.80 + 0.20 * ratio, "正在限制混音峰值…"), total_samples / _AUDIO_RATE)
    return destination


def _prepare_video_windows(context, options, work, cancel_event, report):
    """Cache user-selected windows once so repeated notes loop inside them."""
    if not options.material_selections:
        return context
    assets = dict(context.assets)
    durations = dict(context.durations)
    selections = [(pitch, context.windows[pitch]) for pitch in assets if pitch in context.windows and
                  (context.windows[pitch][0] > 1e-9 or context.windows[pitch][1] < durations.get(pitch, math.inf) - 1e-9)]
    for index, (pitch, (first, last)) in enumerate(selections):
        _check_cancel(cancel_event)
        destination = work / f"selected-video-{pitch:03d}.mp4"
        duration = last - first
        frames = math.floor(duration * options.fps + 1e-8)
        if frames < 1:
            raise RenderError(f"素材 {_pitch_name(pitch)} 的选段不足一帧，请扩大素材范围或提高帧率。")
        # Keep a selected window's exact period, even when its length is not an
        # integer number of output frames. Retiming only spreads the selected
        # frames over that period; it never reads frames beyond the window.
        rate = frames / duration
        filters = f"fps={options.fps},trim=end_frame={frames},setpts=N/({rate:.12f}*TB)"
        command = _command(context.ffmpeg) + ["-ss", f"{first:.9f}", "-threads", "1", "-i", str(context.assets[pitch]),
                   "-map", "0:v:0", "-an", "-vf", filters, "-frames:v", str(frames), "-r", f"{rate:.12f}",
                   "-fps_mode", "cfr", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0", "-pix_fmt", "yuv420p", "-video_track_timescale", "48000", str(destination)]
        _encode(command, cancel_event, expected_seconds=duration)
        assets[pitch] = destination
        durations[pitch] = duration
        report((index + 1) / max(1, len(selections)), f"已缓存素材选段 {index + 1}/{len(selections)}…")
    return replace(context, assets=assets, durations=durations)


def _encode_grid_segment(context, options, segment, destination, script, cancel_event, progress) -> None:
    command = _command(context.ffmpeg) + ["-filter_complex_threads", "1"]
    count = len(segment.voices)
    seconds = segment.frame_count / options.fps
    video_directory = Path(options.video_dir).expanduser().resolve()
    if not count:
        command += ["-f", "lavfi", "-i", f"color=c=black:s={options.width}x{options.height}:r={options.fps}", "-map", "0:v:0"]
    else:
        columns = math.ceil(math.sqrt(count))
        rows = math.ceil(count / columns)
        cell_width = options.width // columns // 2 * 2
        cell_height = options.height // rows // 2 * 2
        if cell_width < 2 or cell_height < 2:
            raise RenderError(f"当前分辨率不足以同时显示 {count} 个音符，请提高输出分辨率。")
        border = 2 if count > 1 and min(cell_width, cell_height) >= 16 else 0
        picture_width, picture_height = cell_width - 2 * border, cell_height - 2 * border
        # Each voice needs its own decoder. Splitting one decoder before trims
        # with distant offsets can buffer the early voice while xstack waits.
        for voice in segment.voices:
            source = context.assets[voice.pitch]
            try:
                relative_source = os.path.relpath(source, video_directory)
            except ValueError:  # Symlink target on another Windows drive.
                relative_source = str(source)
            duration = context.durations.get(voice.pitch)
            offset = voice.source_offset % duration if duration else voice.source_offset
            command += ["-stream_loop", "-1", "-ss", f"{offset:.9f}", "-threads", "1", "-i", relative_source]
        filters = []
        for index, voice in enumerate(segment.voices):
            filters.append(
                f"[{index}:v:0]setpts=PTS-STARTPTS,fps={options.fps},"
                f"scale={picture_width}:{picture_height}:force_original_aspect_ratio=decrease,"
                f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[tile{index}]"
            )
        if count == 1:
            filters.append("[tile0]null[grid]")
        else:
            inputs = "".join(f"[tile{index}]" for index in range(count))
            layout = "|".join(f"{index % columns * cell_width}_{index // columns * cell_height}" for index in range(count))
            filters.append(f"{inputs}xstack=inputs={count}:layout={layout}:fill=black[grid]")
        filters.append(f"[grid]pad={options.width}:{options.height}:(ow-iw)/2:(oh-ih)/2:color=black[vout]")
        command += ["-filter_complex_script", _filter_script(script, filters), "-map", "[vout]"]
    command += ["-an", "-frames:v", str(segment.frame_count), "-t", f"{seconds:.9f}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0", "-pix_fmt", "yuv420p", "-threads", "2", str(destination)]
    _encode(command, cancel_event, progress, seconds, cwd=video_directory)


def _publish(rendered: Path, output: Path, overwrite: bool, cancel_event) -> None:
    _check_cancel(cancel_event)
    if not rendered.is_file() or rendered.stat().st_size == 0:
        raise RenderError("FFmpeg 未生成有效的输出文件。")
    try:
        if overwrite:
            os.replace(rendered, output)
        elif os.name == "nt":
            os.rename(rendered, output)
        else:
            os.link(rendered, output)
            rendered.unlink()
    except FileExistsError as exc:
        raise RenderError("导出期间出现了同名输出文件，已保留该文件。请换一个文件名后重试。") from exc


class _WorkerCancellation:
    def __init__(self, parent):
        self.parent = parent
        self.stopped = threading.Event()

    def is_set(self):
        return self.stopped.is_set() or (self.parent is not None and self.parent.is_set())


def _render_clip_segments(context, options, work, cancel_event, on_progress):
    total_frames = context.segments[-1].end_frame
    requested = options.render_workers or min(4, max(1, (os.cpu_count() or 2) // 2))
    # Bound aggregate decoder buffers as well as CPU parallelism.
    largest_grid = max(1, max(len(segment.voices) for segment in context.segments))
    estimated_job_bytes = max(1, options.width * options.height * largest_grid * 24)
    memory_workers = max(1, (512 * 1024 * 1024) // estimated_job_bytes)
    workers = min(requested, memory_workers, len(context.segments))
    cancellation = _WorkerCancellation(cancel_event)
    fractions = [0.0] * len(context.segments)
    lock = threading.Lock()
    completed_frames = 0.0

    def encode(number, segment):
        _check_cancel(cancellation)
        destination = work / f"segment-{number:06d}.mkv"
        script = work / f"video-grid-{number:06d}.txt"
        label = "休止" if not segment.voices else f"{len(segment.voices)} 个同时音符"
        message = f"并行处理画面 {number + 1}/{len(context.segments)}：{label}"

        def report(ratio):
            nonlocal completed_frames
            with lock:
                fraction = max(fractions[number], ratio)
                completed_frames += segment.frame_count * (fraction - fractions[number])
                fractions[number] = fraction
                _notify(on_progress, 0.25 + 0.65 * completed_frames / total_frames, message)

        _encode_grid_segment(context, options, segment, destination, script, cancellation, report)
        report(1.0)
        return destination

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(encode, number, segment) for number, segment in enumerate(context.segments)]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                cancellation.stopped.set()
                for future in futures:
                    future.cancel()
                raise
    finally:
        cancellation.stopped.set()
    concat_lines = []
    for number, segment in enumerate(context.segments):
        concat_lines += [f"file 'segment-{number:06d}.mkv'", f"duration {segment.frame_count / options.fps:.9f}"]
    manifest = work / "concat.txt"
    manifest.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(manifest)]


def render_midi(
    data: MidiData,
    options: RenderOptions,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Render independent voices with original audio or an imported music track."""
    context = _prepare(data, options, on_progress, cancel_event)
    context.output.parent.mkdir(parents=True, exist_ok=True)
    total_seconds = context.segments[-1].end_frame / options.fps
    with tempfile.TemporaryDirectory(prefix=".midi-stitch-", dir=context.output.parent) as temporary:
        work = Path(temporary)
        audio = _render_audio(context, options, work, cancel_event, lambda value, text: _notify(on_progress, 0.2 * value, text), allow_silence=True)
        if options.video_mode == "labels":
            from label_video import encode_label_video

            video = work / "note-labels.mkv"
            encode_label_video(context.data, context.segments, options, video, work, context.ffmpeg, cancel_event,
                               lambda value: _notify(on_progress, 0.2 + 0.7 * value, "正在生成音名、频率和力度画面…"))
            video_input = ["-i", str(video)]
        else:
            video_context = _prepare_video_windows(context, options, work, cancel_event,
                lambda value, text: _notify(on_progress, 0.2 + 0.05 * value, text))
            video_input = _render_clip_segments(video_context, options, work, cancel_event, on_progress)
        rendered = work / "finished.mp4"
        command = _command(context.ffmpeg) + video_input + ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(_AUDIO_RATE), "-ac", "2", "-t", f"{total_seconds:.9f}", "-movflags", "+faststart", str(rendered)]
        _notify(on_progress, 0.9, "正在合成最终 MP4…")
        _encode(command, cancel_event, lambda ratio: _notify(on_progress, 0.9 + 0.09 * ratio, "正在合成最终 MP4…"), total_seconds)
        _publish(rendered, context.output, options.overwrite, cancel_event)
    _notify(on_progress, 1.0, f"导出完成：{context.output.name}")
    return context.output


def render_preview_audio(
    data: MidiData,
    options: RenderOptions,
    output_path: str | Path,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Write a PCM WAV using the identical original-audio selection and mix."""
    context = _prepare(data, options, on_progress, cancel_event, output_path=output_path, extension=".wav")
    if context.soundtrack is None and not any(options.use_clip_audio and context.audio_by_pitch[voice.pitch] for segment in context.segments for voice in segment.voices):
        raise RenderError("所选音符的视频素材没有可试听的音轨；成片仍可导出静音视频。")
    context.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".midi-stitch-preview-", dir=context.output.parent) as temporary:
        audio = _render_audio(context, options, Path(temporary), cancel_event, lambda value, text: _notify(on_progress, 0.95 * value, text), allow_silence=False)
        _publish(audio, context.output, options.overwrite, cancel_event)
    _notify(on_progress, 1.0, f"试听已生成：{context.output.name}")
    return context.output


def render_audio(data: MidiData, options: RenderOptions, output_path: str | Path,
                 on_progress: Callable[[float, str], None] | None = None,
                 cancel_event: threading.Event | None = None) -> Path:
    """Export the selected original mix or imported music as a standalone WAV."""
    return render_preview_audio(data, options, output_path, on_progress, cancel_event)
