"""Verify a generated sine-material directory, writing tests/asset_verification.json."""
from __future__ import annotations

import argparse
from array import array
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

from generate_sine_assets import NO_WINDOW, frequency, note_name


def pcm(data: bytes) -> array:
    result = array("h")
    result.frombytes(data)
    if sys.byteorder != "little":
        result.byteswap()
    return result


def measured_frequency(samples: array, rate: int) -> float:
    crossings = []
    for index in range(len(samples) - 1):
        first, second = samples[index], samples[index + 1]
        if first <= 0 < second:
            crossings.append(index - first / (second - first))
    if len(crossings) < 3:
        raise ValueError("Not enough complete sine cycles")
    return (len(crossings) - 1) * rate / (crossings[-1] - crossings[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    notes = manifest["notes"]
    pitches = [entry["midi"] for entry in notes]
    assert pitches == list(range(pitches[0], pitches[-1] + 1))
    assert len(list((root / "wav").glob("*.wav"))) == len(notes)
    assert len(list((root / "videos").glob("*.mp4"))) == len(notes)

    def verify(entry: dict) -> dict:
        pitch, seconds = entry["midi"], entry["duration_seconds"]
        name, expected = note_name(pitch), frequency(pitch)
        frames = round(seconds * 48000)
        assert entry["note"] == name and abs(entry["frequency_hz"] - expected) < 1e-10
        assert entry["wav"] == f"wav/{name}.wav" and entry["video"] == f"videos/{name}.mp4"
        with wave.open(str(root / entry["wav"]), "rb") as stream:
            assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(),
                    stream.getnframes(), stream.getcomptype()) == (2, 2, 48000, frames, "NONE"), name
            samples = pcm(stream.readframes(frames))
        assert samples[::2] == samples[1::2], (name, "WAV stereo mismatch")
        mono = samples[::2]
        assert mono[0] == mono[-1] == 0, (name, "WAV edge fade")
        assert max(abs(value) for value in mono) <= 6554, (name, "WAV peak")
        for index in range(0, frames, 97):
            envelope = min(1., index / 480, (frames - 1 - index) / 480)
            reference = round(32767 * .2 * envelope * math.sin(math.tau * expected * index / 48000))
            assert mono[index] == reference, (name, "WAV sine sample", index)
        middle = mono[24000:-24000]
        wav_hz = measured_frequency(middle, 48000)
        assert abs(wav_hz - expected) <= max(.05, expected * .0001), (name, "WAV frequency", wav_hz)

        result = subprocess.run([args.ffprobe, "-v", "error", "-show_streams", "-show_format",
                                 "-of", "json", str(root / entry["video"])], capture_output=True,
                                check=True, creationflags=NO_WINDOW)
        probe = json.loads(result.stdout)
        videos = [stream for stream in probe["streams"] if stream["codec_type"] == "video"]
        audios = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]
        assert len(videos) == len(audios) == 1, name
        video, audio = videos[0], audios[0]
        assert (video["codec_name"], video["width"], video["height"], video["r_frame_rate"],
                int(video["nb_frames"])) == ("h264", 640, 360, "30/1", math.ceil(seconds * 30)), name
        assert (audio["codec_name"], audio["channels"], int(audio["sample_rate"])) == ("aac", 2, 48000), name
        durations = [float(video["duration"]), float(audio["duration"]), float(probe["format"]["duration"])]
        assert all(abs(duration - seconds) <= 1 / 30 + 1e-5 for duration in durations), (name, durations)

        # Decode the whole video as well as audio, detecting truncated or corrupt packets.
        decoded = subprocess.run([
            args.ffmpeg, "-v", "error", "-xerror", "-nostdin", "-threads", "1",
            "-i", str(root / entry["video"]), "-map", "0:v:0", "-t", str(seconds),
            "-threads", "1", "-f", "null", os.devnull,
            "-map", "0:a:0", "-t", str(seconds), "-ac", "1", "-ar", "48000",
            "-f", "s16le", "pipe:1"], capture_output=True, check=True, creationflags=NO_WINDOW)
        decoded_mono = pcm(decoded.stdout)
        assert len(decoded_mono) == frames, (name, "decoded audio frames", len(decoded_mono))
        aac_hz = measured_frequency(decoded_mono[24000:-24000], 48000)
        assert abs(aac_hz - expected) <= max(.05, expected * .0001), (name, "AAC frequency", aac_hz, expected)
        assert max(abs(value) for value in decoded_mono) < 32767, (name, "AAC clipping")
        return {"midi": pitch, "note": name, "expected_hz": expected, "wav_measured_hz": wav_hz,
                "aac_measured_hz": aac_hz, "wav_error_hz": abs(wav_hz - expected),
                "aac_error_hz": abs(aac_hz - expected), "wav_frames": frames,
                "video_frames": int(video["nb_frames"]), "stream_durations_seconds": durations}

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(verify, notes))
    report = {"verified_notes": len(results), "first_note": results[0]["note"],
              "last_note": results[-1]["note"], "max_wav_frequency_error_hz": max(r["wav_error_hz"] for r in results),
              "max_aac_frequency_error_hz": max(r["aac_error_hz"] for r in results),
              "max_duration_error_seconds": max(abs(duration - notes[index]["duration_seconds"])
                  for index, result in enumerate(results) for duration in result["stream_durations_seconds"]),
              "all_videos_fully_decoded": True, "failures": [], "notes": results}
    output = root / "tests/asset_verification.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "notes"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
