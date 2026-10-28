"""Read MIDI notes on their original timeline, including tempo changes and rests."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import mido


_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def note_name(pitch: int) -> str:
    """Return a scientific pitch name: MIDI 60 is C4, MIDI 0 is C-1."""
    if isinstance(pitch, bool) or not isinstance(pitch, int) or not 0 <= pitch <= 127:
        raise ValueError("MIDI 音高必须是 0 到 127 的整数。")
    return f"{_PITCH_CLASSES[pitch % 12]}{pitch // 12 - 1}"


midi_note_to_pitch_name = {pitch: note_name(pitch) for pitch in range(128)}


@dataclass(frozen=True)
class NoteEvent:
    pitch: int
    start: float
    end: float
    velocity: int
    channel: int
    track: int
    source_offset: float = 0.0

    @property
    def name(self) -> str:
        return note_name(self.pitch)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class TrackInfo:
    index: int
    name: str
    channels: tuple[int, ...]
    note_count: int


@dataclass(frozen=True)
class MidiData:
    notes: tuple[NoteEvent, ...]
    tracks: tuple[TrackInfo, ...]
    duration: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _NoteStart:
    pitch: int
    seconds: float
    velocity: int
    channel: int
    track: int


def time_in_s(delta_ticks: int | float, tempo: int, ticks_per_beat: int) -> float:
    """Convert ticks at one constant tempo without losing sub-frame precision."""
    if ticks_per_beat <= 0 or tempo <= 0:
        raise ValueError("每拍 tick 数和 tempo 必须大于 0。")
    return delta_ticks * tempo / (ticks_per_beat * 1_000_000)


def _validate_selection(track: int | None, channel: int | None, track_count: int) -> None:
    if track is not None and (
        isinstance(track, bool) or not isinstance(track, int) or not 0 <= track < track_count
    ):
        raise ValueError(f"轨道索引无效：{track!r}。本文件有 {track_count} 个轨道，索引从 0 开始。")
    if channel is not None and (
        isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 15
    ):
        raise ValueError("通道索引必须是 0 到 15 的整数，或使用 None 选择所有通道。")


def load_midi(path: str | Path, track: int | None = None, channel: int | None = None) -> MidiData:
    """Parse a type 0/1 MIDI file and optionally select one track and/or channel.

    Indices are zero-based. Tempo and pedal messages are read globally before
    selection, so filtering never moves notes or loses a conductor-track tempo
    change. Duration always includes the full MIDI intro, rests and trailing
    end-of-track silence.

    Overlapping note-ons of one pitch pair with note-offs in FIFO order. Sustain
    pedal, reset controllers, all sound off, and all notes off are respected.
    Unmatched note-offs are ignored with a warning. Notes lacking a closing
    event end at the end of the file.
    """
    try:
        midi = mido.MidiFile(path)
    except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"无法读取 MIDI 文件“{path}”：{exc}") from exc

    if midi.type == 2:
        raise ValueError("暂不支持 Type 2 MIDI：其中的轨道是独立序列，请先转换为 Type 0 或 Type 1。")
    if midi.ticks_per_beat <= 0:
        raise ValueError("暂不支持 SMPTE 时间码 MIDI，请先转换为按拍计时的 MIDI。")
    _validate_selection(track, channel, len(midi.tracks))

    events: list[tuple[int, int, int, mido.Message | mido.MetaMessage]] = []
    track_names: list[str] = []
    track_channels: list[set[int]] = []
    for track_index, messages in enumerate(midi.tracks):
        absolute_tick = 0
        name = f"轨道 {track_index}"
        channels: set[int] = set()
        for message_index, message in enumerate(messages):
            if message.time < 0:
                raise ValueError(f"轨道 {track_index} 含有负数事件时间，无法建立播放时间线。")
            absolute_tick += message.time
            events.append((absolute_tick, track_index, message_index, message))
            if message.type == "track_name" and message.name.strip():
                name = message.name.strip()
            if message.type == "note_on" and message.velocity > 0:
                channels.add(message.channel)
        track_names.append(name)
        track_channels.append(channels)

    # Secondary keys make simultaneous events deterministic. A tempo message
    # applies after its own tick, including when it is in a conductor track.
    events.sort(key=lambda event: event[:3])
    active: dict[tuple[int, int, int], deque[_NoteStart]] = defaultdict(deque)
    sustained: dict[int, list[_NoteStart]] = defaultdict(list)
    pedal_down: dict[int, bool] = defaultdict(bool)
    notes: list[NoteEvent] = []
    unmatched_offs = 0
    zero_length = 0
    dangling = 0

    def finish(start: _NoteStart, end: float) -> None:
        nonlocal zero_length
        if end <= start.seconds:
            zero_length += 1
            return
        notes.append(NoteEvent(start.pitch, start.seconds, end, start.velocity, start.channel, start.track))

    def release_pedal(selected_channel: int, seconds: float) -> None:
        for start in sustained.pop(selected_channel, []):
            finish(start, seconds)

    def release_channel(selected_channel: int, seconds: float, immediate: bool) -> None:
        for key in [key for key in active if key[1] == selected_channel]:
            for start in active.pop(key):
                if pedal_down[selected_channel] and not immediate:
                    sustained[selected_channel].append(start)
                else:
                    finish(start, seconds)
        if immediate:
            release_pedal(selected_channel, seconds)

    tempo = 500_000
    previous_tick = 0
    seconds = 0.0
    for absolute_tick, track_index, _, message in events:
        seconds += time_in_s(absolute_tick - previous_tick, tempo, midi.ticks_per_beat)
        previous_tick = absolute_tick
        if message.type == "set_tempo":
            if message.tempo <= 0:
                raise ValueError("MIDI 中包含无效的 tempo（必须大于 0）。")
            tempo = message.tempo
        elif message.type == "note_on" and message.velocity > 0:
            key = (track_index, message.channel, message.note)
            active[key].append(_NoteStart(message.note, seconds, message.velocity, message.channel, track_index))
        elif message.type in ("note_on", "note_off"):
            key = (track_index, message.channel, message.note)
            queue = active.get(key)
            if not queue:
                unmatched_offs += 1
                continue
            start = queue.popleft()
            if not queue:
                active.pop(key)
            if pedal_down[message.channel]:
                sustained[message.channel].append(start)
            else:
                finish(start, seconds)
        elif message.type == "control_change":
            if message.control == 64:
                if message.value < 64:
                    release_pedal(message.channel, seconds)
                pedal_down[message.channel] = message.value >= 64
            elif message.control == 120:
                release_channel(message.channel, seconds, immediate=True)
            elif message.control == 121:
                pedal_down[message.channel] = False
                release_pedal(message.channel, seconds)
            elif message.control == 123:
                release_channel(message.channel, seconds, immediate=False)

    for queue in active.values():
        for start in queue:
            dangling += 1
            finish(start, seconds)
    for starts in sustained.values():
        for start in starts:
            dangling += 1
            finish(start, seconds)

    notes.sort(key=lambda note: (note.start, note.track, note.channel, note.pitch, note.end))
    counts = [0] * len(midi.tracks)
    for note in notes:
        counts[note.track] += 1
    tracks = tuple(
        TrackInfo(index, track_names[index], tuple(sorted(track_channels[index])), counts[index])
        for index in range(len(midi.tracks))
    )
    warnings: list[str] = []
    if unmatched_offs:
        warnings.append(f"已忽略 {unmatched_offs} 个没有对应起音的结束事件。")
    if dangling:
        warnings.append(f"{dangling} 个音符未正常结束，已在 MIDI 末尾关闭。")
    if zero_length:
        warnings.append(f"已忽略 {zero_length} 个没有正持续时间的音符。")
    selected = tuple(
        note for note in notes
        if (track is None or note.track == track) and (channel is None or note.channel == channel)
    )
    return MidiData(selected, tracks, seconds, tuple(warnings))


def midi_note_to_durations(midfile, selected_track=0, chanel=0):
    """Compatibility wrapper returning [[pitch_name, seconds], ...]."""
    data = load_midi(midfile, track=selected_track, channel=chanel)
    return [[note.name, note.duration] for note in data.notes]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="读取 MIDI 音符及持续时间。")
    parser.add_argument("midi", help="MIDI 文件路径")
    parser.add_argument("--track", type=int, default=0, help="轨道索引（从 0 开始）")
    parser.add_argument("--channel", type=int, default=0, help="通道索引（0–15）")
    args = parser.parse_args()
    try:
        print(midi_note_to_durations(args.midi, args.track, args.channel))
    except ValueError as exc:
        parser.exit(1, f"错误：{exc}\n")
