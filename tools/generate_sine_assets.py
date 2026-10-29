"""Create labeled sine-wave WAV/MP4 materials; Python standard library + FFmpeg.

Example: python tools/generate_sine_assets.py --output ../sine_test_MIDI_0_127_5s
The output directory must be new: existing materials are never overwritten.
"""
from __future__ import annotations

import argparse
from array import array
import colorsys
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
RATE, PEAK, FADE_SECONDS = 48000, .20, .01
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def note_name(pitch: int) -> str:
    return f"{NAMES[pitch % 12]}{pitch // 12 - 1}"


def frequency(pitch: int) -> float:
    return 440. * 2. ** ((pitch - 69) / 12.)


def vlq(value: int) -> bytes:
    result = [value & 127]
    while value >> 7:
        value >>= 7
        result.insert(0, (value & 127) | 128)
    return bytes(result)


def write_midi(path: Path, notes: list[tuple[float, float, int, int]], end: float) -> None:
    # 480 ticks/quarter at 120 bpm = 960 ticks/second. Off events precede on events.
    events = []
    for start, finish, pitch, velocity in notes:
        events.extend(((round(start * 960), 1, bytes((0x90, pitch, velocity))),
                       (round(finish * 960), 0, bytes((0x80, pitch, 0)))))
    track = bytearray(b"\x00\xff\x51\x03\x07\xa1\x20")
    previous = 0
    for tick, _, message in sorted(events):
        track.extend(vlq(tick - previous) + message)
        previous = tick
    track.extend(vlq(max(previous, round(end * 960)) - previous) + b"\xff\x2f\x00")
    path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
                     + b"MTrk" + struct.pack(">I", len(track)) + track)


def find_font(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else [
        Path("C:/Windows/Fonts/consola.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/System/Library/Fonts/Menlo.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("No label font found. Supply --font /path/to/monospace.ttf")


def write_wav(path: Path, hz: float, frames: int, envelope: list[float]) -> None:
    mono = array("h", (round(32767 * PEAK * envelope[i] * math.sin(math.tau * hz * i / RATE))
                       for i in range(frames)))
    stereo = array("h", [0]) * (2 * frames)
    stereo[::2] = mono
    stereo[1::2] = mono
    if sys.byteorder != "little":
        stereo.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(stereo.tobytes())


def run(command: list[str], **kwargs) -> None:
    result = subprocess.run(command, capture_output=True, creationflags=NO_WINDOW, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-4000:])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first", type=int, default=0, help="First MIDI pitch (default 0 = C-1)")
    parser.add_argument("--last", type=int, default=127, help="Last MIDI pitch (default 127 = G9)")
    parser.add_argument("--duration", type=float, default=5.)
    parser.add_argument("--workers", type=int, default=2, help="Concurrent encoders; each uses one thread")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--font", help="Monospaced TrueType font (auto-detected on Windows/Linux/macOS)")
    args = parser.parse_args()
    if not 0 <= args.first <= args.last <= 127:
        parser.error("MIDI range must satisfy 0 <= first <= last <= 127")
    if not math.isfinite(args.duration) or not .02 <= args.duration <= 3600:
        parser.error("duration must be between 0.02 and 3600 seconds")
    if not 1 <= args.workers <= 8:
        parser.error("workers must be between 1 and 8")
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        parser.error("FFmpeg was not found. Install it or supply --ffmpeg /path/to/ffmpeg")
    font = find_font(args.font)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for directory in ("wav", "videos", "tests"):
        (root / directory).mkdir()
    frames = round(args.duration * RATE)
    seconds = frames / RATE
    fade = round(FADE_SECONDS * RATE)
    envelope = [min(1., i / fade, (frames - 1 - i) / fade) for i in range(frames)]
    notes = []
    for pitch in range(args.first, args.last + 1):
        name, hz = note_name(pitch), frequency(pitch)
        write_wav(root / "wav" / f"{name}.wav", hz, frames, envelope)
        notes.append({"midi": pitch, "note": name, "frequency_hz": hz,
                      "duration_seconds": seconds, "wav": f"wav/{name}.wav",
                      "video": f"videos/{name}.mp4"})
    print(f"Created {len(notes)} WAV tones", flush=True)

    with tempfile.TemporaryDirectory(prefix=".sine-labels-", dir=root) as temporary:
        scratch = Path(temporary)
        shutil.copyfile(font, scratch / "font.ttf")
        for entry in notes:
            pitch = entry["midi"]
            (scratch / f"{pitch}-note.txt").write_text(entry["note"], encoding="utf-8")
            (scratch / f"{pitch}-info.txt").write_text(
                f"{entry['frequency_hz']:.5f} Hz  |  MIDI {pitch}", encoding="utf-8")

        def render(entry: dict) -> str:
            pitch = entry["midi"]
            rgb = colorsys.hsv_to_rgb(((pitch % 12) / 12 + .58) % 1, .62, .26)
            color = "0x" + "".join(f"{round(component * 255):02x}" for component in rgb)
            filters = (
                "drawbox=x=28:y=28:w=584:h=304:color=white@0.2:t=2,"
                f"drawtext=fontfile=font.ttf:textfile={pitch}-note.txt:fontsize=100:fontcolor=white:x=(w-tw)/2:y=82,"
                f"drawtext=fontfile=font.ttf:textfile={pitch}-info.txt:fontsize=24:fontcolor=white:x=(w-tw)/2:y=226,"
                f"drawtext=fontfile=font.ttf:text='{seconds:.2f} s  SINE':fontsize=20:fontcolor=white@0.72:x=(w-tw)/2:y=278"
            )
            run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                 "-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=30",
                 "-i", str(root / entry["wav"]), "-vf", filters, "-t", str(seconds),
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "1", "-filter_threads", "1",
                 "-c:a", "aac", "-b:a", "160k", "-ar", str(RATE), "-ac", "2",
                 "-movflags", "+faststart", "-metadata",
                 f"title={entry['note']} | {entry['frequency_hz']:.5f} Hz | {seconds:g}s sine",
                 str(root / entry["video"])], cwd=scratch)
            return entry["note"]

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(render, entry) for entry in notes]
            for count, future in enumerate(as_completed(futures), 1):
                name = future.result()
                if count % 16 == 0 or count == len(notes):
                    print(f"Rendered {count}/{len(notes)} videos (latest {name})", flush=True)

    all_notes = [(index * .5, (index + 1) * .5, entry["midi"], 100)
                 for index, entry in enumerate(notes)]
    write_midi(root / "tests/test_all_notes.mid", all_notes, len(notes) * .5)
    middle = max(args.first, min(60, args.last))
    chord = sorted(set(max(args.first, min(pitch, args.last)) for pitch in (60, 64, 67, 71)))
    overlaps = [(index, index + 5, pitch, 100 - index * 12) for index, pitch in enumerate(chord)]
    overlaps.extend(((3.5, 6.5, middle, 72), (8.5, 10, args.first, 100),
                     (8.5, 10, args.last, 100)))
    write_midi(root / "tests/test_overlap.mid", overlaps, 11)
    metadata = {"description": f"{len(notes)} individual {seconds:g}-second sine tones, "
                               f"{notes[0]['note']} through {notes[-1]['note']} inclusive",
                "tuning": "A4=440 Hz, 12-tone equal temperament, MIDI 60=C4",
                "sample_rate": RATE, "channels": 2, "bits_per_sample": 16,
                "peak_amplitude": PEAK, "edge_fade_seconds": FADE_SECONDS,
                "video": {"codec": "h264", "width": 640, "height": 360, "fps": 30, "audio_codec": "aac"},
                "notes": notes}
    (root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "使用说明.txt").write_text(
        f"{seconds:g} 秒正弦音测试素材：{notes[0]['note']} 至 {notes[-1]['note']}，共 {len(notes)} 个音高。\n\n"
        f"wav：每个音高一个 WAV，精确 {seconds:g} 秒，48 kHz / 16 位 / 双声道。\n"
        "videos：同名 MP4，640×360 / 30 fps，彩色底、白框、大音名、频率与 MIDI 编号；带正弦原声。\n"
        "调音：A4 = 440 Hz，十二平均律，MIDI 60 = C4。\n"
        "各音峰值 20%（约 -14 dBFS），两端各 10 毫秒淡入淡出。\n\n"
        "在 MIDI 视频拼接器中，素材目录选择本文件夹下的 videos，画面选择“原视频分屏”。\n"
        f"tests/test_all_notes.mid：逐个测试全部 {len(notes)} 个音高，每音 0.5 秒，共 {len(notes) * .5:g} 秒。\n"
        "tests/test_overlap.mid：11 秒测试，包含和弦、同音重叠、不同力度、最低/最高音。\n"
        "测试 MIDI 是时间线；每份源 WAV、MP4 都保留完整时长。\n"
        "metadata.json：全部音名、MIDI 编号与精确频率。tests/asset_verification.json：素材校验结果。\n\n"
        "最低音 C-1 约 8.176 Hz，最高音 G9 约 12543.854 Hz。超低音可能听不见或设备无法重现，"
        "这是音高本身的范围；请用正常音量测试。\n", encoding="utf-8")
    print(f"Complete: {root}", flush=True)


if __name__ == "__main__":
    main()
