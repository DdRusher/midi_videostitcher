"""Plan reproducible excerpts of each pitch's source material.

The renderer physically selects each source window first. Planned offsets are
relative to that window, so video and audio can share the same loop and excerpt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
import random

from notes import MidiData, note_name


@dataclass(frozen=True)
class MaterialSelection:
    """Use a fixed start or choose a fitting random excerpt inside [start, end)."""

    start: float = 0.0
    end: float | None = None
    mode: str = "fixed"


def _pitch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 127:
        raise ValueError("素材音高必须是 0 到 127 之间的 MIDI 整数。")
    return value


def _seconds(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限的秒数。")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label}必须是有限的秒数。") from error
    if not math.isfinite(result):
        raise ValueError(f"{label}必须是有限的秒数。")
    return result


def _settings(selections: Mapping[int, MaterialSelection] | None) -> dict[int, MaterialSelection]:
    if selections is None:
        return {}
    if not isinstance(selections, Mapping):
        raise ValueError("请按 MIDI 音高提供素材选段设置。")
    result = {}
    for pitch, setting in selections.items():
        _pitch(pitch)
        name = note_name(pitch)
        if not isinstance(setting, MaterialSelection):
            raise ValueError(f"{name} 的素材设置必须是 MaterialSelection。")
        if setting.mode not in ("fixed", "random"):
            raise ValueError(f"{name} 的素材选段方式必须是 fixed（固定）或 random（随机）。")
        start = _seconds(setting.start, f"{name} 素材开始时间")
        end = None if setting.end is None else _seconds(setting.end, f"{name} 素材结束时间")
        if start < 0 or (end is not None and end <= start):
            raise ValueError(f"{name} 的素材选段应满足 0 ≤ 开始时间 < 结束时间。")
        result[pitch] = MaterialSelection(start, end, setting.mode)
    return result


def material_windows(
    selections: Mapping[int, MaterialSelection] | None,
    durations: Mapping[int, float],
) -> dict[int, tuple[float, float]]:
    """Validate the available materials and return absolute source windows.

    Settings for currently unused pitches are retained by the caller and ignored
    here after key/type validation. This permits narrowing a MIDI track or range
    without discarding earlier per-material settings. A blank end uses the end
    of that material. No source window extends beyond its actual duration.
    """
    settings = _settings(selections)
    if not isinstance(durations, Mapping):
        raise ValueError("请按 MIDI 音高提供素材时长。")
    windows = {}
    for pitch, value in durations.items():
        _pitch(pitch)
        name = note_name(pitch)
        duration = _seconds(value, f"{name} 素材时长")
        if duration <= 0:
            raise ValueError(f"{name} 的素材时长必须大于 0 秒。")
        setting = settings.get(pitch, MaterialSelection())
        start = setting.start
        end = duration if setting.end is None else setting.end
        if not 0 <= start < end <= duration:
            raise ValueError(
                f"{name} 的素材选段超出范围：请选择 0 ≤ 开始时间 < 结束时间 ≤ {duration:.6f} 秒。"
            )
        windows[pitch] = (start, end)
    return windows


def plan_material_offsets(
    data: MidiData,
    selections: Mapping[int, MaterialSelection] | None,
    durations: Mapping[int, float],
    seed: int = 0,
) -> MidiData:
    """Return independent, deterministic source positions for selected notes.

    Fixed mode starts at the chosen window's beginning. Random mode leaves room
    for both a note's pre-existing crop offset and its retained duration, when
    those fit in the window. A shorter window starts at zero and repeats in the
    renderer. Existing offsets remain unmodded: they represent continuation of
    a held note, and the renderer owns wraparound within the physical window.

    Calls with the same MIDI data, settings, durations and integer seed produce
    the same offsets. A local RNG avoids changing application-global randomness
    or relying on Python's process-randomized hash values.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("随机选段种子必须是整数。")
    settings = _settings(selections)
    windows = material_windows(settings, durations)
    generator = random.Random(seed)
    planned = []
    for note in data.notes:
        pitch = _pitch(note.pitch)
        name = note_name(pitch)
        if pitch not in windows:
            raise ValueError(f"缺少 {name} 的素材时长，无法安排素材选段。")
        start = _seconds(note.start, f"{name} 音符开始时间")
        end = _seconds(note.end, f"{name} 音符结束时间")
        offset = _seconds(note.source_offset, f"{name} 素材续播偏移")
        if start < 0 or end <= start:
            raise ValueError(f"{name} 的音符起止时间无效。")
        if offset < 0:
            raise ValueError(f"{name} 的素材续播偏移不能为负数。")
        needed = _seconds(offset + end - start, f"{name} 所需素材时长")
        window_start, window_end = windows[pitch]
        setting = settings.get(pitch, MaterialSelection())
        if setting.mode == "random":
            available = max(0.0, (window_end - window_start) - needed)
            offset += generator.uniform(0.0, available)
        planned.append(replace(note, source_offset=offset))
    return replace(data, notes=tuple(planned))
