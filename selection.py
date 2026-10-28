"""Select MIDI tracks, channels and a time range without losing source position."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import math
import re

from notes import MidiData, NoteEvent


_SECONDS = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_INTEGER = re.compile(r"[0-9]+\Z")


def parse_time(text: str) -> float:
    """Parse seconds, mm:ss.sss or hh:mm:ss.sss as a nonnegative duration.

    In mm:ss, minutes may exceed 59 (for example, 90:00). In hh:mm:ss,
    minutes must be below 60. Seconds in either colon format must be below 60.
    Whitespace around the complete value is ignored; empty values are invalid.
    """
    message = "时间格式应为秒、分:秒或时:分:秒，例如 12.5、01:30 或 01:02:03.5。"
    if not isinstance(text, str) or not text.strip():
        raise ValueError(message)
    parts = text.strip().split(":")
    if len(parts) not in (1, 2, 3):
        raise ValueError(message)
    if not _SECONDS.fullmatch(parts[-1]) or any(not _INTEGER.fullmatch(part) for part in parts[:-1]):
        raise ValueError(message)
    try:
        seconds = float(parts[-1])
        if len(parts) >= 2:
            if seconds >= 60:
                raise ValueError("使用分:秒或时:分:秒格式时，秒数必须小于 60。")
            minutes = int(parts[-2])
            if len(parts) == 3 and minutes >= 60:
                raise ValueError("使用时:分:秒格式时，分钟必须小于 60。")
            seconds += minutes * 60
        if len(parts) == 3:
            seconds += int(parts[0]) * 3600
    except OverflowError as exc:
        raise ValueError("时间数值过大，请输入有限的时间。") from exc
    if not math.isfinite(seconds):
        raise ValueError("时间必须是有限的非负数。")
    return seconds


def _number(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name}必须是有限的秒数。")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}必须是有限的秒数。") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"{name}必须是有限的秒数。")
    return seconds


def _indices(values: Iterable[int] | None, name: str, available: set[int]) -> set[int] | None:
    if values is None:
        return None
    try:
        requested = list(values)
    except TypeError as exc:
        raise ValueError(f"请使用{name}索引列表进行选择。") from exc
    if not requested:
        raise ValueError(f"请至少选择一个{name}。")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in requested):
        raise ValueError(f"{name}索引必须是整数。")
    selected = set(requested)
    invalid = sorted(selected - available)
    if invalid:
        if name == "通道":
            raise ValueError("通道索引必须在 0 到 15 之间。")
        raise ValueError(f"所选轨道不存在：{'、'.join(str(value) for value in invalid)}。")
    return selected


def select_midi(
    data: MidiData,
    tracks: Iterable[int] | None = None,
    channels: Iterable[int] | None = None,
    start: float = 0.0,
    end: float | None = None,
) -> MidiData:
    """Return a selection with zero-based time and retained media offsets.

    Track and channel filters intersect. None selects all tracks or channels;
    an explicitly empty selection is an error. The selected interval is
    half-open [start, end), with leading and trailing rests retained. Metadata
    stays unchanged so a UI may keep displaying the complete file's track list.
    Repeated selections add to each note's source_offset, measured in seconds.
    """
    duration = _number(data.duration, "MIDI 时长")
    begin = _number(start, "开始时间")
    finish = duration if end is None else _number(end, "结束时间")
    if duration <= 0:
        raise ValueError("MIDI 时长必须大于 0。")
    if not 0 <= begin < finish <= duration:
        raise ValueError(
            f"请选择有效时间范围：0 ≤ 开始时间 < 结束时间 ≤ MIDI 时长（{duration:.6f} 秒）。"
        )
    chosen_tracks = _indices(tracks, "轨道", {info.index for info in data.tracks})
    chosen_channels = _indices(channels, "通道", set(range(16)))
    selected: list[NoteEvent] = []
    for note in data.notes:
        if chosen_tracks is not None and note.track not in chosen_tracks:
            continue
        if chosen_channels is not None and note.channel not in chosen_channels:
            continue
        note_start = _number(note.start, "音符开始时间")
        note_end = _number(note.end, "音符结束时间")
        if note_start < 0 or note_end < note_start or note_end > duration:
            raise ValueError("MIDI 中包含超出时间线范围的音符。")
        clipped_start = max(begin, note_start)
        clipped_end = min(finish, note_end)
        if clipped_end <= clipped_start:
            continue
        source_offset = _number(note.source_offset, "素材起始偏移")
        if source_offset < 0:
            raise ValueError("素材起始偏移不能为负数。")
        continued_offset = _number(source_offset + max(begin - note_start, 0.0), "素材起始偏移")
        selected.append(replace(
            note,
            start=clipped_start - begin,
            end=clipped_end - begin,
            source_offset=continued_offset,
        ))
    if not selected:
        raise ValueError("所选轨道、通道和时间范围内没有可用音符；请扩大选区或调整筛选。")
    return MidiData(tuple(selected), data.tracks, finish - begin, data.warnings)
