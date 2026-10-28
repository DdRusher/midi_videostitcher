"""Timeline invariants, safe output handling, and a small real FFmpeg export."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from renderer import (
    RenderCancelled,
    RenderError,
    RenderOptions,
    build_timeline,
    render_midi,
    validate_assets,
)


def note(pitch: int, start: float, end: float):
    return SimpleNamespace(pitch=pitch, start=start, end=end)


def song(*notes, duration=1.0):
    return SimpleNamespace(notes=notes, duration=duration)


class TimelineTests(unittest.TestCase):
    def test_chords_keep_song_time_and_resume_underlying_note(self):
        data = song(note(60, 0.1, 0.8), note(64, 0.3, 0.5), note(67, 0.4, 0.6))
        timeline = build_timeline(data, 10, "highest")
        self.assertEqual(
            [(s.start_frame, s.end_frame, s.pitch) for s in timeline],
            [(0, 1, None), (1, 3, 60), (3, 4, 64), (4, 6, 67), (6, 8, 60), (8, 10, None)],
        )
        self.assertEqual(timeline[-2].source_offset_frames, 5)
        self.assertEqual(sum(s.frame_count for s in timeline), 10)

    def test_lowest_note_merges_across_suppressed_chords(self):
        data = song(note(60, 0.1, 0.8), note(64, 0.3, 0.5), note(67, 0.4, 0.6))
        self.assertEqual(
            [(s.start_frame, s.end_frame, s.pitch) for s in build_timeline(data, 10, "lowest")],
            [(0, 1, None), (1, 8, 60), (8, 10, None)],
        )

    def test_latest_can_select_lower_pitch(self):
        data = song(note(72, 0, 1), note(60, 0.3, 0.5))
        self.assertEqual(
            [(s.start_frame, s.end_frame, s.pitch) for s in build_timeline(data, 10, "latest")],
            [(0, 3, 72), (3, 5, 60), (5, 10, 72)],
        )

    def test_repeated_same_pitch_restarts_material(self):
        timeline = build_timeline(song(note(60, 0, 0.3), note(60, 0.3, 0.6), duration=0.6), 10)
        self.assertEqual(len(timeline), 2)
        self.assertEqual([s.source_offset_frames for s in timeline], [0, 0])

    def test_short_notes_do_not_accumulate_rounding_error(self):
        notes = [note(60, i * 0.013, (i + 1) * 0.013) for i in range(100)]
        timeline = build_timeline(song(*notes, duration=1.3), 30)
        self.assertEqual(sum(s.frame_count for s in timeline), 39)
        self.assertEqual(timeline[0].start_frame, 0)
        self.assertEqual(timeline[-1].end_frame, 39)
        self.assertTrue(all(left.end_frame == right.start_frame for left, right in zip(timeline, timeline[1:])))

    def test_fractional_tail_is_at_most_one_frame_longer(self):
        timeline = build_timeline(song(note(60, 0, 0.5), duration=0.501), 30)
        self.assertEqual(timeline[-1].end_frame, 16)

    def test_all_zero_frame_notes_fail_instead_of_exporting_a_black_song(self):
        data = song(note(60, 0.001, 0.002), note(62, 0.051, 0.052))
        with self.assertRaisesRegex(RenderError, "全部 2 个音符.*提高帧率"):
            build_timeline(data, 20)

    def test_invalid_time_or_empty_selection_is_explained(self):
        for data in (song(), song(note(60, -1, 0.5)), song(note(60, 0, float("nan")))):
            with self.subTest(data=data), self.assertRaises(RenderError):
                build_timeline(data)
        with self.assertRaises(RenderError):
            build_timeline(song(note(60, 0, 1)), 0)


class AssetAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_flat_alias_and_numeric_filename(self):
        (self.directory / "Db4.MP4").touch()
        (self.directory / "60.mov").touch()
        mapping = validate_assets(song(note(60, 0, 0.5), note(61, 0.5, 1)), self.directory)
        self.assertEqual(mapping[60].name, "60.mov")
        self.assertEqual(mapping[61].name, "Db4.MP4")

    def test_missing_material_lists_all_missing_notes(self):
        with self.assertRaises(RenderError) as error:
            validate_assets(song(note(60, 0, 0.5), note(62, 0.5, 1)), self.directory)
        self.assertIn("C4", str(error.exception))
        self.assertIn("D4", str(error.exception))

    def test_conflicting_aliases_are_not_chosen_arbitrarily(self):
        (self.directory / "C4.mp4").touch()
        (self.directory / "60.mp4").touch()
        with self.assertRaisesRegex(RenderError, "多个素材"):
            validate_assets(song(note(60, 0, 1)), self.directory)

    def test_cancellation_happens_before_output_is_created(self):
        cancelled = threading.Event()
        cancelled.set()
        output = self.directory / "out.mp4"
        with self.assertRaises(RenderCancelled):
            render_midi(song(note(60, 0, 1)), RenderOptions(self.directory, output), cancel_event=cancelled)
        self.assertFalse(output.exists())

    def test_all_zero_frame_notes_fail_before_creating_or_replacing_output(self):
        output = self.directory / "previous.mp4"
        output.write_bytes(b"previous export")
        data = song(note(60, 0.001, 0.002), note(62, 0.051, 0.052))
        with patch("renderer.find_ffmpeg") as find:
            with self.assertRaisesRegex(RenderError, "全部 2 个音符.*提高帧率"):
                render_midi(data, RenderOptions(self.directory, output, fps=20, overwrite=True))
        find.assert_not_called()
        self.assertEqual(output.read_bytes(), b"previous export")
        self.assertFalse(list(self.directory.glob(".midi-stitch-*")))

    def test_existing_output_is_preserved_without_overwrite(self):
        (self.directory / "C4.mp4").touch()
        output = self.directory / "previous.mp4"
        output.write_bytes(b"previous export")
        with self.assertRaisesRegex(RenderError, "已经存在"):
            render_midi(song(note(60, 0, 1)), RenderOptions(self.directory, output))
        self.assertEqual(output.read_bytes(), b"previous export")

    def test_cannot_overwrite_material_even_when_overwrite_is_enabled(self):
        source = self.directory / "C4.mp4"
        source.write_bytes(b"source video")
        with self.assertRaisesRegex(RenderError, "不能覆盖"):
            render_midi(song(note(60, 0, 1)), RenderOptions(self.directory, source, overwrite=True))
        self.assertEqual(source.read_bytes(), b"source video")

    def test_cannot_overwrite_soundtrack(self):
        (self.directory / "C4.mp4").touch()
        audio = self.directory / "music.mp4"
        audio.write_bytes(b"source soundtrack")
        with self.assertRaisesRegex(RenderError, "不能覆盖"):
            render_midi(song(note(60, 0, 1)), RenderOptions(self.directory, audio, audio_path=audio, overwrite=True))
        self.assertEqual(audio.read_bytes(), b"source soundtrack")

    def test_encoder_error_does_not_damage_old_output(self):
        (self.directory / "C4.mp4").touch()
        output = self.directory / "old.mp4"
        output.write_bytes(b"old export")
        with patch("renderer.find_ffmpeg", return_value="ffmpeg"), patch("renderer._probe", return_value=(True, False)), patch("renderer._encode", side_effect=RenderError("encoder failed")):
            with self.assertRaisesRegex(RenderError, "encoder failed"):
                render_midi(song(note(60, 0, 1)), RenderOptions(self.directory, output, overwrite=True))
        self.assertEqual(output.read_bytes(), b"old export")
        self.assertFalse(list(self.directory.glob(".midi-stitch-*")))

    def test_keyboard_interrupt_stops_child_process(self):
        from renderer import _run_process

        process = SimpleNamespace(
            communicate=lambda **kwargs: (b"", b""),
            poll=lambda: None,
        )
        from unittest.mock import Mock

        process.terminate = Mock()
        process.kill = Mock()
        with patch("renderer.subprocess.Popen", return_value=process), patch("renderer._check_cancel", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                _run_process(["ffmpeg"], None)
        process.terminate.assert_called_once()

    def test_cancellation_stops_running_child_process(self):
        from renderer import _run_process

        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        self.addCleanup(timer.cancel)
        original_popen = subprocess.Popen
        children = []

        def record_child(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child

        timer.start()
        with patch("renderer.subprocess.Popen", side_effect=record_child):
            with self.assertRaises(RenderCancelled):
                _run_process([sys.executable, "-c", "import time; time.sleep(5)"], cancelled)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are not installed")
class RealExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.ffmpeg = shutil.which("ffmpeg")
        self.flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for filename, color in (("C4.mp4", "red"), ("D4.mp4", "green")):
            subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s=64x64:r=20", "-t", "0.1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.directory / filename)],
                check=True, capture_output=True, creationflags=self.flags,
            )

    def frame_color(self, path: Path, time: float):
        data = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(time), "-i", str(path), "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            check=True, capture_output=True, creationflags=self.flags,
        ).stdout
        self.assertEqual(len(data), 3)
        return tuple(data)

    def audio_rms(self, path: Path, time: float):
        import array
        import math

        data = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(time), "-i", str(path), "-t", "0.03", "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"],
            check=True, capture_output=True, creationflags=self.flags,
        ).stdout
        samples = array.array("h", data)
        self.assertGreater(len(samples), 0)
        return math.sqrt(sum(value * value for value in samples) / len(samples))

    def test_real_export_loops_short_material_and_preserves_rest_positions(self):
        output = self.directory / "导出 video.mp4"
        data = song(note(60, 0.15, 0.45), note(62, 0.6, 0.8), duration=1.0)
        progress = []
        render_midi(data, RenderOptions(self.directory, output, width=64, height=64, fps=20), lambda value, text: progress.append(value))
        self.assertTrue(output.is_file())
        report = json.loads(subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            check=True, capture_output=True, text=True, creationflags=self.flags,
        ).stdout)
        self.assertAlmostEqual(float(report["format"]["duration"]), 1.0, delta=0.06)
        self.assertEqual({stream["codec_type"] for stream in report["streams"]}, {"video", "audio"})
        self.assertTrue(all(a <= b + 1e-9 for a, b in zip(progress, progress[1:])))
        self.assertEqual(progress[-1], 1)
        for time in (0.05, 0.5, 0.9):
            with self.subTest(time=time):
                self.assertTrue(all(channel < 20 for channel in self.frame_color(output, time)))
        red = self.frame_color(output, 0.25)
        self.assertGreater(red[0], 180)
        self.assertLess(red[1], 40)
        green = self.frame_color(output, 0.65)
        self.assertGreater(green[1], 80)
        self.assertLess(green[0], 40)

    def test_short_soundtrack_is_padded_with_silence(self):
        music = self.directory / "short music.wav"
        subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.1:sample_rate=48000", str(music)],
            check=True, capture_output=True, creationflags=self.flags,
        )
        output = self.directory / "with music.mp4"
        render_midi(song(note(60, 0, 1)), RenderOptions(self.directory, output, width=64, height=64, fps=20, audio_path=music))
        self.assertGreater(self.audio_rms(output, 0.02), 500)
        self.assertLess(self.audio_rms(output, 0.5), 30)

    def test_dropped_note_warning_matches_quantization_without_changing_export(self):
        baseline = song(note(60, 0.15, 0.45))
        with_short_note = song(note(60, 0.15, 0.45), note(62, 0.51, 0.52))
        ordinary_output = self.directory / "ordinary.mp4"
        short_note_output = self.directory / "short note.mp4"
        render_midi(baseline, RenderOptions(self.directory, ordinary_output, width=64, height=64, fps=20))
        progress = []
        render_midi(
            with_short_note,
            RenderOptions(self.directory, short_note_output, width=64, height=64, fps=20),
            lambda value, text: progress.append((value, text)),
        )
        self.assertEqual(progress[0], (0.0, "有 1 个音符短于当前帧率可表示的时长，未显示；可提高帧率。"))
        self.assertTrue(all(a[0] <= b[0] + 1e-9 for a, b in zip(progress, progress[1:])))
        self.assertEqual(progress[-1][0], 1.0)
        for output in (ordinary_output, short_note_output):
            report = json.loads(subprocess.run(
                [shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                check=True, capture_output=True, text=True, creationflags=self.flags,
            ).stdout)
            video = next(stream for stream in report["streams"] if stream["codec_type"] == "video")
            self.assertEqual(int(video["nb_frames"]), 20)
            self.assertAlmostEqual(float(report["format"]["duration"]), 1.0, delta=0.06)
            self.assertTrue(all(channel < 20 for channel in self.frame_color(output, 0.5)))
        decoded_audio = [subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(output), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"],
            check=True, capture_output=True, creationflags=self.flags,
        ).stdout for output in (ordinary_output, short_note_output)]
        self.assertEqual(decoded_audio[0], decoded_audio[1])

    def test_warning_counts_collapsed_boundaries_instead_of_all_sub_frame_durations(self):
        data = song(note(60, 0.2, 0.6), note(62, 0.02, 0.03), note(62, 0.051, 0.052))
        progress = []
        output = self.directory / "boundary warning.mp4"
        render_midi(data, RenderOptions(self.directory, output, width=64, height=64, fps=20), lambda value, text: progress.append((value, text)))
        self.assertEqual(progress[0], (0.0, "有 1 个音符短于当前帧率可表示的时长，未显示；可提高帧率。"))
        green = self.frame_color(output, 0.0)
        self.assertGreater(green[1], 80)
        self.assertLess(green[0], 40)

    def test_original_material_audio_plays_only_during_note(self):
        subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=red:s=64x64:r=20", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.directory / "C4.mp4")],
            check=True, capture_output=True, creationflags=self.flags,
        )
        output = self.directory / "material audio.mp4"
        render_midi(song(note(60, 0.2, 0.6)), RenderOptions(self.directory, output, width=64, height=64, fps=20))
        self.assertLess(self.audio_rms(output, 0.03), 30)
        self.assertGreater(self.audio_rms(output, 0.3), 500)
        self.assertLess(self.audio_rms(output, 0.8), 30)

    def test_cancel_during_export_keeps_old_output_and_cleans_temporary_files(self):
        output = self.directory / "previous.mp4"
        output.write_bytes(b"previous export")
        cancelled = threading.Event()

        def cancel_after_first_segment(value, message):
            if value > 0:
                cancelled.set()

        with self.assertRaises(RenderCancelled):
            render_midi(
                song(note(60, 0.2, 0.6)),
                RenderOptions(self.directory, output, width=64, height=64, fps=20, overwrite=True),
                cancel_after_first_segment,
                cancelled,
            )
        self.assertEqual(output.read_bytes(), b"previous export")
        self.assertFalse(list(self.directory.glob(".midi-stitch-*")))


if __name__ == "__main__":
    unittest.main()
