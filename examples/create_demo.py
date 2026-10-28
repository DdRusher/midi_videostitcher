"""Generate a small, original MIDI/video fixture for trying the application."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def create_demo(output_dir: str | Path | None = None) -> tuple[Path, Path]:
    import mido
    from renderer import find_ffmpeg

    destination = Path(output_dir) if output_dir is not None else Path.cwd() / "midi_videostitcher_demo"
    destination = destination.resolve()
    videos = destination / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    midi_path = destination / "demo.mid"
    if not midi_path.exists():
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        conductor = mido.MidiTrack()
        conductor.append(mido.MetaMessage("track_name", name="Tempo"))
        conductor.append(mido.MetaMessage("set_tempo", tempo=500000))
        conductor.append(mido.MetaMessage("set_tempo", tempo=666667, time=960))
        conductor.append(mido.MetaMessage("end_of_track", time=2400))
        melody = mido.MidiTrack()
        melody.append(mido.MetaMessage("track_name", name="Demo melody"))
        melody.append(mido.Message("program_change", program=0, channel=0))
        events = []
        for start, end, pitch in (
            (480, 960, 60), (960, 1440, 64), (1200, 1680, 67),
            (1920, 2160, 60), (2160, 2640, 64), (2640, 2880, 67),
        ):
            events.append((start, mido.Message("note_on", note=pitch, velocity=90)))
            events.append((end, mido.Message("note_off", note=pitch, velocity=0)))
        previous = 0
        for tick, message in sorted(events, key=lambda item: (item[0], item[1].type == "note_on")):
            melody.append(message.copy(time=tick - previous))
            previous = tick
        melody.append(mido.MetaMessage("end_of_track", time=3360 - previous))
        midi.tracks.extend((conductor, melody))
        midi.save(midi_path)
    executable = find_ffmpeg()
    for name, color, frequency in (
        ("C4", "0xe74c3c", 261.626),
        ("E4", "0x2ecc71", 329.628),
        ("G4", "0x3498db", 391.995),
    ):
        source = videos / f"{name}.mp4"
        if source.exists():
            continue
        command = [
            executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=30",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
            "-t", "0.4", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
            "-movflags", "+faststart", str(source),
        ]
        result = subprocess.run(command, capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            source.unlink(missing_ok=True)
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return midi_path, videos


def main() -> int:
    parser = argparse.ArgumentParser(description="生成含变速、休止和重叠音符的演示素材。")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--render", action="store_true", help="同时导出 demonstration.mp4")
    args = parser.parse_args()
    midi, videos = create_demo(args.output_dir)
    print(f"MIDI: {midi}\n素材目录: {videos}")
    if args.render:
        from notes import load_midi
        from renderer import RenderOptions, render_midi

        output = midi.parent / "demonstration.mp4"
        render_midi(load_midi(midi, track=1),
                    RenderOptions(videos, output, width=640, height=360, overwrite=True),
                    on_progress=lambda progress, message: print(f"{progress:.0%} {message}", flush=True))
        print(f"成片: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
