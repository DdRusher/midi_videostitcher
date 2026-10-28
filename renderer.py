"""Render a MIDI timeline with local note videos, using FFmpeg only.

MIDI times are quantized against one absolute frame clock.  Concurrent notes
remain independent voices, with a dynamic video grid and one continuous mix.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import heapq
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING, Callable

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


@dataclass(frozen=True)
class _AudioInterval:
    note_id: int
    pitch: int
    first: int
    last: int
    source_offset: float


@dataclass(frozen=True)
class _AudioLayer:
    path: Path
    first_sample: int
    last_sample: int


_AUDIO_RATE = 48000
_AUDIO_BATCH = 24


_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
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


def validate_assets(data: MidiData, video_dir: str | Path) -> dict[int, Path]:
    """Map pitches to files, rejecting missing notes and ambiguous filenames.

    A note can be named C#4, Db4, or 61, with a supported video extension.
    Matching is case insensitive.  Multiple aliases for one pitch are rejected
    so changing file-system enumeration order cannot change the rendered song.
    """
    directory = Path(video_dir).expanduser()
    if not directory.is_dir():
        raise RenderError(f"视频素材文件夹不存在：{directory}")
    entries = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in _EXTENSIONS),
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
        first, last = _note_frame_bounds(start, end, total, fps)
        source_offset = float(getattr(note, "source_offset", 0.0))
        if not math.isfinite(source_offset) or source_offset < 0:
            raise RenderError("素材起始偏移必须是有限的非负秒数。")
        if last <= first:
            skipped += 1
            continue
        notes[index] = _QuantizedNote(index, pitch, first, last, start, source_offset)
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
        TimelineVoice(index, notes[index].pitch, notes[index].source_offset + (first - notes[index].first) / fps)
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


def _probe(ffmpeg: str, path: Path, cancel_event: threading.Event | None) -> tuple[bool, bool]:
    _, _, stderr = _run_process([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], cancel_event)
    text = stderr.decode("utf-8", errors="replace")
    has_video = bool(re.search(r"Stream #.*?: Video:", text))
    has_audio = bool(re.search(r"Stream #.*?: Audio:", text))
    return has_video, has_audio


def _validate_options(options: RenderOptions, output_path: str | Path | None = None, extension: str = ".mp4") -> Path:
    for value, label in ((options.width, "宽度"), (options.height, "高度")):
        if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 7680 or value % 2:
            raise RenderError(f"视频{label}必须是 16 到 7680 之间的偶数。")
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
    assets = validate_assets(data, options.video_dir)
    soundtrack: Path | None = None
    if options.audio_path:
        soundtrack = Path(options.audio_path).expanduser().resolve()
        if not soundtrack.is_file():
            raise RenderError(f"配乐文件不存在：{soundtrack}")
    protected = [*assets.values(), *([soundtrack] if soundtrack else [])]
    for path in protected:
        if output == path or (output.exists() and output.samefile(path)):
            raise RenderError("输出文件不能覆盖输入的视频素材或配乐，请换一个输出路径。")
    if output.exists() and not options.overwrite:
        raise RenderError(f"输出文件已经存在：{output}。请换一个文件名，或允许覆盖。")
    ffmpeg = find_ffmpeg(options.ffmpeg_path)
    audio_by_pitch: dict[int, bool] = {}
    for pitch, source in assets.items():
        _check_cancel(cancel_event)
        _notify(on_progress, 0.0, f"检查素材：{source.name}")
        has_video, has_audio = _probe(ffmpeg, source, cancel_event)
        if not has_video:
            raise RenderError(f"素材没有可读取的视频画面：{source.name}")
        audio_by_pitch[pitch] = has_audio
        if not has_audio and options.use_clip_audio and soundtrack is None:
            _notify(on_progress, 0.0, f"素材 {source.name} 没有音轨，该音符将保持静音。")
    if soundtrack and not _probe(ffmpeg, soundtrack, cancel_event)[1]:
        raise RenderError(f"配乐文件中没有可读取的音轨：{soundtrack.name}")
    _check_cancel(cancel_event)
    return _RenderContext(output, segments, assets, audio_by_pitch, ffmpeg, soundtrack)


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
                intervals.append(_AudioInterval(voice.note_id, voice.pitch, segment.start_frame, segment.end_frame, voice.source_offset))
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


def _mix_voice_batch(context, options, intervals, work, number, cancel_event, progress) -> _AudioLayer:
    fps = options.fps
    first = min(_sample_at_frame(interval.first, fps) for interval in intervals)
    last = max(_sample_at_frame(interval.last, fps) for interval in intervals)
    command = _command(context.ffmpeg)
    filters = []
    for index, interval in enumerate(intervals):
        command += ["-stream_loop", "-1", "-i", str(context.assets[interval.pitch])]
        offset = math.floor(interval.source_offset * _AUDIO_RATE + 0.5)
        start = _sample_at_frame(interval.first, fps)
        end = _sample_at_frame(interval.last, fps)
        filters.append(
            f"[{index}:a:0]aresample={_AUDIO_RATE}:async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=start_sample={offset}:end_sample={offset + end - start},"
            f"asetpts=PTS-STARTPTS,adelay={start - first}S:all=1[voice{index}]"
        )
    labels = "".join(f"[voice{index}]" for index in range(len(intervals)))
    filters.append(f"{labels}amix=inputs={len(intervals)}:duration=longest:dropout_transition=0:normalize=0,apad,atrim=end_sample={last - first}[mix]")
    destination = work / f"audio-voices-{number:06d}.wav"
    command += ["-filter_complex_script", _filter_script(work / f"audio-voices-{number:06d}.txt", filters), "-map", "[mix]", "-vn", "-c:a", "pcm_f32le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-rf64", "auto", str(destination)]
    _encode(command, cancel_event, progress, (last - first) / _AUDIO_RATE)
    return _AudioLayer(destination, first, last)


def _mix_layers(context, layers, destination, script, cancel_event, progress, *, total_samples=None, final=False) -> _AudioLayer:
    first = 0 if total_samples is not None else min(layer.first_sample for layer in layers)
    last = total_samples if total_samples is not None else max(layer.last_sample for layer in layers)
    command = _command(context.ffmpeg)
    filters = []
    for index, layer in enumerate(layers):
        command += ["-i", str(layer.path)]
        filters.append(f"[{index}:a:0]asetpts=PTS-STARTPTS,adelay={layer.first_sample - first}S:all=1[layer{index}]")
    labels = "".join(f"[layer{index}]" for index in range(len(layers)))
    ending = "alimiter=limit=0.95:level=false:latency=true:attack=5:release=50," if final else ""
    filters.append(
        f"{labels}amix=inputs={len(layers)}:duration=longest:dropout_transition=0:normalize=0,"
        f"apad,atrim=end_sample={last - first},{ending}apad,atrim=end_sample={last - first}[mix]"
    )
    command += ["-filter_complex_script", _filter_script(script, filters), "-map", "[mix]", "-vn", "-c:a", "pcm_s16le" if final else "pcm_f32le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-rf64", "auto", str(destination)]
    _encode(command, cancel_event, progress, (last - first) / _AUDIO_RATE)
    return _AudioLayer(destination, first, last)


def _render_audio(context, options, work, cancel_event, report, *, allow_silence: bool) -> Path:
    """One continuous mix is shared by preview and video, with one limiter."""
    total_samples = _sample_at_frame(context.segments[-1].end_frame, options.fps)
    destination = work / "mixed-original-audio.wav"
    if context.soundtrack is not None:
        # Kept for old CLI/API callers; the desktop uses original clip audio.
        filters = (
            f"aresample={_AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,"
            f"apad,atrim=end_sample={total_samples},alimiter=limit=0.95:level=false:latency=true,"
            f"apad,atrim=end_sample={total_samples}"
        )
        command = _command(context.ffmpeg) + ["-i", str(context.soundtrack), "-map", "0:a:0", "-vn", "-af", filters, "-c:a", "pcm_s16le", "-ar", str(_AUDIO_RATE), "-ac", "2", "-rf64", "auto", str(destination)]
        _encode(command, cancel_event, lambda ratio: report(ratio, "正在处理配乐…"), total_samples / _AUDIO_RATE)
        return destination
    intervals = [interval for interval in _audio_intervals(context.segments) if options.use_clip_audio and context.audio_by_pitch[interval.pitch]]
    if not intervals:
        if not allow_silence:
            if not options.use_clip_audio:
                raise RenderError("素材原声已关闭，无法生成有声试听。")
            raise RenderError("所选音符的视频素材没有可试听的音轨；成片仍可导出静音视频。")
        _write_silence(destination, total_samples, cancel_event, report)
        return destination
    group_count = math.ceil(len(intervals) / _AUDIO_BATCH)
    jobs = group_count + 1
    remaining = group_count
    while remaining > _AUDIO_BATCH:
        remaining = math.ceil(remaining / _AUDIO_BATCH)
        jobs += remaining
    finished = 0

    def job_progress(ratio):
        report((finished + ratio) / jobs, f"正在混合素材原声 {finished + 1}/{jobs}…")

    layers = []
    for index in range(group_count):
        _check_cancel(cancel_event)
        group = intervals[index * _AUDIO_BATCH:(index + 1) * _AUDIO_BATCH]
        layers.append(_mix_voice_batch(context, options, group, work, index, cancel_event, job_progress))
        finished += 1
    generation = 0
    while len(layers) > _AUDIO_BATCH:
        combined = []
        for index in range(0, len(layers), _AUDIO_BATCH):
            _check_cancel(cancel_event)
            path = work / f"audio-layer-{generation}-{index}.wav"
            combined.append(_mix_layers(context, layers[index:index + _AUDIO_BATCH], path, path.with_suffix(".txt"), cancel_event, job_progress))
            finished += 1
        layers = combined
        generation += 1
    _mix_layers(context, layers, destination, work / "audio-final.txt", cancel_event, job_progress, total_samples=total_samples, final=True)
    return destination


def _encode_grid_segment(context, options, segment, destination, script, cancel_event, progress) -> None:
    command = _command(context.ffmpeg)
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
            command += ["-stream_loop", "-1", "-i", relative_source]
        filters = []
        for index, voice in enumerate(segment.voices):
            filters.append(
                f"[{index}:v:0]trim=start={voice.source_offset:.9f},setpts=PTS-STARTPTS,fps={options.fps},"
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


def render_midi(
    data: MidiData,
    options: RenderOptions,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Render every active voice in a grid with the same mix as audio preview."""
    context = _prepare(data, options, on_progress, cancel_event)
    context.output.parent.mkdir(parents=True, exist_ok=True)
    total_frames = context.segments[-1].end_frame
    total_seconds = total_frames / options.fps
    with tempfile.TemporaryDirectory(prefix=".midi-stitch-", dir=context.output.parent) as temporary:
        work = Path(temporary)
        audio = _render_audio(context, options, work, cancel_event, lambda value, text: _notify(on_progress, 0.2 * value, text), allow_silence=True)
        concat_lines = []
        for number, segment in enumerate(context.segments):
            _check_cancel(cancel_event)
            destination = work / f"segment-{number:06d}.mkv"
            script = work / f"video-grid-{number:06d}.txt"
            label = "休止" if not segment.voices else f"{len(segment.voices)} 个同时音符"
            message = f"处理画面 {number + 1}/{len(context.segments)}：{label}"
            _notify(on_progress, 0.2 + 0.7 * segment.start_frame / total_frames, message)

            def report(ratio, current=segment, text=message):
                _notify(on_progress, 0.2 + 0.7 * (current.start_frame + ratio * current.frame_count) / total_frames, text)

            _encode_grid_segment(context, options, segment, destination, script, cancel_event, report)
            concat_lines += [f"file '{destination.name}'", f"duration {segment.frame_count / options.fps:.9f}"]
        manifest = work / "concat.txt"
        manifest.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        rendered = work / "finished.mp4"
        command = _command(context.ffmpeg) + ["-f", "concat", "-safe", "0", "-i", str(manifest), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(_AUDIO_RATE), "-ac", "2", "-t", f"{total_seconds:.9f}", "-movflags", "+faststart", str(rendered)]
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
