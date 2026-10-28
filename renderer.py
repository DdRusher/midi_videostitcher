"""Render a MIDI timeline with local note videos, using FFmpeg only.

MIDI times are quantized against one absolute frame clock.  Chords select one
active note instead of extending the song by concatenating overlapping notes.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    chord_policy: str = "highest"
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

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


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
        return candidate
    candidate = shutil.which("ffmpeg")
    if candidate:
        return candidate
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


def build_timeline(data: MidiData, fps: int = 30, chord_policy: str = "highest") -> tuple[TimelineSegment, ...]:
    """Build frame-aligned segments with rests and a deterministic chord policy."""
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 120:
        raise RenderError("帧率必须是 1 到 120 之间的整数。")
    if chord_policy not in {"highest", "lowest", "latest"}:
        raise RenderError("和弦处理方式必须是 highest、lowest 或 latest。")
    duration = float(data.duration)
    if not math.isfinite(duration) or duration <= 0:
        raise RenderError("MIDI 时长必须大于零。")
    if not data.notes:
        raise RenderError("所选轨道或通道没有可导出的音符。")
    total = max(1, math.ceil(duration * fps - 1e-9))
    starts: dict[int, list[int]] = {}
    ends: dict[int, list[int]] = {}
    note_frames: dict[int, tuple[int, int, int, float]] = {}
    for index, note in enumerate(data.notes):
        start, end = float(note.start), float(note.end)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise RenderError("MIDI 中包含无效的音符起止时间。")
        pitch = int(note.pitch)
        if not 0 <= pitch <= 127:
            raise RenderError(f"MIDI 音高超出范围：{pitch}")
        first, last = _note_frame_bounds(start, end, total, fps)
        if last <= first:
            continue
        note_frames[index] = (first, last, pitch, start)
        starts.setdefault(first, []).append(index)
        ends.setdefault(last, []).append(index)
    if not note_frames:
        raise RenderError(f"全部 {len(data.notes)} 个音符在当前帧率下都不足一帧，无法生成可见音符画面；请提高帧率。")
    boundaries = sorted({0, total, *starts, *ends})
    active: set[int] = set()
    heap: list[tuple[float, float, int, int]] = []
    segments: list[TimelineSegment] = []
    previous_note: int | None = None
    for first, last in zip(boundaries, boundaries[1:]):
        for index in ends.get(first, ()):
            active.discard(index)
        for index in starts.get(first, ()):
            active.add(index)
            _, _, pitch, start = note_frames[index]
            if chord_policy == "highest":
                key = (-pitch, -start, -index, index)
            elif chord_policy == "lowest":
                key = (pitch, -start, -index, index)
            else:
                key = (-start, -pitch, -index, index)
            heapq.heappush(heap, key)
        while heap and heap[0][3] not in active:
            heapq.heappop(heap)
        selected = heap[0][3] if heap else None
        pitch = note_frames[selected][2] if selected is not None else None
        offset = first - note_frames[selected][0] if selected is not None else 0
        if segments and selected == previous_note and segments[-1].end_frame == first:
            old = segments[-1]
            segments[-1] = TimelineSegment(old.start_frame, last, old.pitch, old.source_offset_frames)
        else:
            segments.append(TimelineSegment(first, last, pitch, offset))
        previous_note = selected
    return tuple(segments)


def _run_process(
    command: list[str],
    cancel_event: threading.Event | None,
    progress: Callable[[float], None] | None = None,
    expected_seconds: float = 0.0,
) -> tuple[int, bytes, bytes]:
    _check_cancel(cancel_event)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
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


def _encode(command: list[str], cancel_event: threading.Event | None, progress=None, expected_seconds=0.0) -> None:
    code, _, stderr = _run_process(command, cancel_event, progress, expected_seconds)
    if code:
        detail = stderr.decode("utf-8", errors="replace")[-5000:].strip()
        raise RenderError(f"FFmpeg 导出失败：\n{detail}")


def _probe(ffmpeg: str, path: Path, cancel_event: threading.Event | None) -> tuple[bool, bool]:
    _, _, stderr = _run_process([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], cancel_event)
    text = stderr.decode("utf-8", errors="replace")
    has_video = bool(re.search(r"Stream #.*?: Video:", text))
    has_audio = bool(re.search(r"Stream #.*?: Audio:", text))
    return has_video, has_audio


def _validate_options(options: RenderOptions) -> Path:
    for value, label in ((options.width, "宽度"), (options.height, "高度")):
        if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 7680 or value % 2:
            raise RenderError(f"视频{label}必须是 16 到 7680 之间的偶数。")
    if not str(options.output_path).strip():
        raise RenderError("请选择输出文件。")
    output = Path(options.output_path).expanduser().resolve()
    if output.suffix.lower() != ".mp4":
        raise RenderError("输出文件请使用 .mp4 扩展名。")
    if output.is_dir():
        raise RenderError("输出路径指向文件夹，请选择一个 .mp4 文件名。")
    return output


def render_midi(
    data: MidiData,
    options: RenderOptions,
    on_progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Render an MP4 and publish it only after every encoding step succeeds."""
    _check_cancel(cancel_event)
    output = _validate_options(options)
    segments = build_timeline(data, options.fps, options.chord_policy)
    total_frames = segments[-1].end_frame
    skipped_notes = 0
    for note in data.notes:
        first, last = _note_frame_bounds(float(note.start), float(note.end), total_frames, options.fps)
        if last <= first:
            skipped_notes += 1
    if skipped_notes:
        _notify(on_progress, 0.0, f"有 {skipped_notes} 个音符短于当前帧率可表示的时长，未显示；可提高帧率。")
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
    if soundtrack and not _probe(ffmpeg, soundtrack, cancel_event)[1]:
        raise RenderError(f"配乐文件中没有可读取的音轨：{soundtrack.name}")
    _check_cancel(cancel_event)
    output.parent.mkdir(parents=True, exist_ok=True)
    fps = options.fps
    total_seconds = total_frames / fps
    with tempfile.TemporaryDirectory(prefix=".midi-stitch-", dir=output.parent) as temporary:
        work = Path(temporary)
        concat_lines: list[str] = []
        for number, segment in enumerate(segments):
            _check_cancel(cancel_event)
            seconds = segment.frame_count / fps
            destination = work / f"segment-{number:06d}.mkv"
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
            if segment.pitch is None:
                label = "休止"
                command += ["-f", "lavfi", "-i", f"color=c=black:s={options.width}x{options.height}:r={fps}"]
                source_audio = False
                offset = 0.0
            else:
                label = _pitch_name(segment.pitch)
                command += ["-stream_loop", "-1", "-i", str(assets[segment.pitch])]
                source_audio = options.use_clip_audio and soundtrack is None and audio_by_pitch[segment.pitch]
                offset = segment.source_offset_frames / fps
            if not source_audio:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            video_filter = (
                f"trim=start={offset:.9f},setpts=PTS-STARTPTS,fps={fps},"
                f"scale={options.width}:{options.height}:force_original_aspect_ratio=decrease,"
                f"pad={options.width}:{options.height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p"
            )
            audio_filter = (
                f"atrim=start={offset if source_audio else 0:.9f},asetpts=PTS-STARTPTS,"
                "aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,apad"
            )
            command += [
                "-map", "0:v:0", "-map", "0:a:0" if source_audio else "1:a:0",
                "-vf", video_filter, "-af", audio_filter,
                "-frames:v", str(segment.frame_count), "-t", f"{seconds:.9f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0", "-threads", "2",
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(destination),
            ]
            message = f"处理片段 {number + 1}/{len(segments)}：{label}"
            _notify(on_progress, 0.9 * segment.start_frame / total_frames, message)

            def segment_progress(ratio: float, current=segment, text=message) -> None:
                _notify(on_progress, 0.9 * (current.start_frame + ratio * current.frame_count) / total_frames, text)

            _encode(command, cancel_event, segment_progress, seconds)
            concat_lines += [f"file '{destination.name}'", f"duration {seconds:.9f}"]
        manifest = work / "concat.txt"
        manifest.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        rendered = work / "finished.mp4"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", "-f", "concat", "-safe", "0", "-i", str(manifest)]
        if soundtrack:
            command += ["-i", str(soundtrack)]
        command += [
            "-map", "0:v:0", "-map", "1:a:0" if soundtrack else "0:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-af", f"apad,atrim=duration={total_seconds:.9f},asetpts=PTS-STARTPTS",
            "-t", f"{total_seconds:.9f}", "-movflags", "+faststart", str(rendered),
        ]
        _notify(on_progress, 0.9, "正在合成最终 MP4…")
        _encode(command, cancel_event, lambda ratio: _notify(on_progress, 0.9 + 0.09 * ratio, "正在合成最终 MP4…"), total_seconds)
        _check_cancel(cancel_event)
        if not rendered.is_file() or rendered.stat().st_size == 0:
            raise RenderError("FFmpeg 未生成有效的输出文件。")
        try:
            if options.overwrite:
                os.replace(rendered, output)
            elif os.name == "nt":
                os.rename(rendered, output)
            else:
                os.link(rendered, output)
                rendered.unlink()
        except FileExistsError as exc:
            raise RenderError("导出期间出现了同名输出文件，已保留该文件。请换一个文件名后重试。") from exc
    _notify(on_progress, 1.0, f"导出完成：{output.name}")
    return output
