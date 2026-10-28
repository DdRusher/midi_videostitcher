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
    build_polyphonic_timeline,
    build_timeline,
    render_preview_audio,
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

    def test_polyphonic_timeline_retains_independent_same_pitch_voices(self):
        first = SimpleNamespace(pitch=60, start=0.0, end=0.8, source_offset=0.2)
        second = SimpleNamespace(pitch=60, start=0.3, end=0.6, source_offset=0.0)
        timeline = build_polyphonic_timeline(song(first, second), 10)
        self.assertEqual([len(segment.voices) for segment in timeline], [1, 2, 1, 0])
        overlap = timeline[1]
        self.assertEqual([voice.note_id for voice in overlap.voices], [0, 1])
        self.assertAlmostEqual(overlap.voices[0].source_offset, 0.5)
        self.assertEqual(overlap.voices[1].source_offset, 0.0)
        self.assertAlmostEqual(timeline[2].voices[0].source_offset, 0.8)

    def test_legacy_timeline_retains_selected_media_offset(self):
        data = song(SimpleNamespace(pitch=60, start=0, end=0.5, source_offset=1.25))
        self.assertEqual(build_timeline(data)[0].source_offset, 1.25)


class FilterScriptCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.command = [str(self.directory / "modern" / "ffmpeg.exe"), "-i", "input.mp4",
                        "-filter_complex_script", str(self.directory / "filter_complex_script.txt"),
                        "-map", "[vout]", "output.mp4"]
        cache = patch.dict("renderer._FILTER_SCRIPT_OPTIONS", {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        self.rejection = (1, b"", b"Unrecognized option 'filter_complex_script'.\nError splitting the argument list: Option not found\n")

    def test_old_ffmpeg_keeps_legacy_option_without_retry(self):
        from renderer import _encode

        original = self.command.copy()
        progress = []
        with patch("renderer._run_process", return_value=(0, b"", b"")) as run:
            _encode(self.command, None, progress.append)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], original)
        self.assertEqual(self.command, original)
        self.assertEqual(progress, [1.0])

    def test_removed_option_retries_exact_token_and_preserves_context(self):
        from renderer import _encode

        cancellation = threading.Event()
        original = self.command.copy()
        with patch("renderer._run_process", side_effect=[self.rejection, (0, b"", b"")]) as run:
            _encode(self.command, cancellation, expected_seconds=4.5, cwd=self.directory)
        self.assertEqual(run.call_count, 2)
        expected = original.copy()
        expected[3] = "-/filter_complex"
        self.assertEqual(run.call_args_list[0].args[0], original)
        self.assertEqual(run.call_args_list[1].args[0], expected)
        for call in run.call_args_list:
            self.assertEqual(call.args[1:], (cancellation, None, 4.5, self.directory))
        self.assertEqual(self.command, original)

    def test_modern_choice_is_cached_per_absolute_executable_path(self):
        from renderer import _encode

        second_binary = self.command.copy()
        second_binary[0] = str(self.directory / "older" / "ffmpeg.exe")
        with patch("renderer._run_process", side_effect=[self.rejection, (0, b"", b""),
                                                        (0, b"", b""), (0, b"", b"")]) as run:
            _encode(self.command, None)
            _encode(self.command, None)
            _encode(second_binary, None)
        self.assertEqual(run.call_args_list[2].args[0][3], "-/filter_complex")
        self.assertEqual(run.call_args_list[3].args[0][3], "-filter_complex_script")

    def test_real_encoder_and_similar_option_errors_are_not_retried(self):
        from renderer import _encode

        for error in (b"No such filter: filter_complex_script",
                      b"Unrecognized option 'filter_complex_script_extra'.",
                      b"Error opening input file filter_complex_script."):
            with self.subTest(error=error), patch("renderer._run_process", return_value=(1, b"", error)) as run:
                with self.assertRaises(RenderError):
                    _encode(self.command, None)
                run.assert_called_once()

    def test_rejection_without_legacy_argument_is_not_retried(self):
        from renderer import _encode

        with patch("renderer._run_process", return_value=self.rejection) as run:
            with self.assertRaises(RenderError):
                _encode([self.command[0], "-i", "input.mp4", "output.mp4"], None)
        run.assert_called_once()

    def test_failed_attempt_does_not_emit_completion_before_retry(self):
        from renderer import _encode

        progress = []

        def execute(command, cancel, callback, seconds, cwd):
            if "-filter_complex_script" in command:
                callback(1.0)
                return self.rejection
            callback(0.5)
            callback(1.0)
            return 0, b"", b""

        with patch("renderer._run_process", side_effect=execute):
            _encode(self.command, None, progress.append)
        self.assertEqual(progress, [0.5, 1.0])

    def test_modern_encoder_failure_is_reported_without_another_retry(self):
        from renderer import _encode

        progress = []
        with patch("renderer._run_process", side_effect=[self.rejection, (1, b"", b"modern encoder failed")]) as run:
            with self.assertRaisesRegex(RenderError, "modern encoder failed"):
                _encode(self.command, None, progress.append)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(progress, [])

    def test_cancel_after_option_rejection_prevents_retry(self):
        from renderer import _encode

        cancellation = threading.Event()

        def reject(*args):
            cancellation.set()
            return self.rejection

        with patch("renderer._run_process", side_effect=reject) as run:
            with self.assertRaises(RenderCancelled):
                _encode(self.command, cancellation)
        run.assert_called_once()


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

    def test_partial_missing_audio_track_is_reported_before_rendering(self):
        from renderer import _prepare

        (self.directory / "C4.mp4").touch()
        (self.directory / "D4.mp4").touch()
        progress = []
        options = RenderOptions(self.directory, self.directory / "output.mp4")
        with patch("renderer.find_ffmpeg", return_value="ffmpeg"), patch("renderer._probe", side_effect=[(True, True), (True, False)]):
            context = _prepare(song(note(60, 0, 1), note(62, 0, 1)), options, lambda value, text: progress.append((value, text)), None)
        self.assertTrue(context.audio_by_pitch[60])
        self.assertFalse(context.audio_by_pitch[62])
        self.assertIn((0.0, "素材 D4.mp4 没有音轨，该音符将保持静音。"), progress)
        self.assertFalse((self.directory / "output.mp4").exists())

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


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
class PolyphonicExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.ffmpeg = shutil.which("ffmpeg")
        self.flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run_ffmpeg(self, arguments):
        return subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *arguments],
            check=True, capture_output=True, creationflags=self.flags,
        ).stdout

    def make_asset(self, filename, colors=("red",), frequencies=(400,), duration=0.8, *, audio=True, impulse=None):
        import array
        import math
        import wave

        wave_path = self.directory / (Path(filename).stem + "-source.wav")
        count = round(duration * 48000)
        samples = array.array("h")
        for index in range(count):
            if impulse is not None:
                value = 12000 if index == impulse else 0
            else:
                frequency = frequencies[min(len(frequencies) - 1, index * len(frequencies) // count)]
                value = round(3000 * math.sin(2 * math.pi * frequency * index / 48000))
            samples.extend((value, value))
        with wave.open(str(wave_path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(samples.tobytes())
        command = []
        for color in colors:
            command += ["-f", "lavfi", "-i", f"color={color}:s=64x64:r=20:d={duration / len(colors):.9f}"]
        if audio:
            command += ["-i", str(wave_path)]
        if len(colors) > 1:
            graph = "".join(f"[{index}:v:0]" for index in range(len(colors))) + f"concat=n={len(colors)}:v=1:a=0[v]"
            command += ["-filter_complex_threads", "1", "-filter_complex", graph, "-map", "[v]"]
        else:
            command += ["-map", "0:v:0"]
        if audio:
            command += ["-map", f"{len(colors)}:a:0", "-c:a", "pcm_s16le"]
        command += ["-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.directory / filename)]
        self.run_ffmpeg(command)
        return self.directory / filename

    def options(self, output, **kwargs):
        return RenderOptions(self.directory, self.directory / output, width=128, height=128, fps=20, **kwargs)

    def pixel(self, path, time, x, y):
        raw = self.run_ffmpeg(["-ss", str(time), "-i", str(path), "-frames:v", "1", "-vf", f"format=rgb24,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        self.assertEqual(len(raw), 3)
        return tuple(raw)

    def audio_samples(self, path, time=0.0, duration=None):
        import array

        command = ["-ss", str(time), "-i", str(path)]
        if duration is not None:
            command += ["-t", str(duration)]
        command += ["-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"]
        return array.array("h", self.run_ffmpeg(command))

    def amplitude(self, path, time, frequency, duration=0.05):
        import math

        samples = self.audio_samples(path, time, duration)
        self.assertGreater(len(samples), 0)
        omega = 2 * math.pi * frequency / 48000
        real = sum(value * math.cos(omega * index) for index, value in enumerate(samples))
        imaginary = sum(value * math.sin(omega * index) for index, value in enumerate(samples))
        return 2 * math.hypot(real, imaginary) / len(samples)

    def assert_red(self, pixel):
        self.assertGreater(pixel[0], 180)
        self.assertLess(pixel[1], 40)
        self.assertLess(pixel[2], 40)

    def assert_green(self, pixel):
        self.assertGreater(pixel[1], 80)
        self.assertLess(pixel[0], 40)
        self.assertLess(pixel[2], 40)

    def assert_blue(self, pixel):
        self.assertGreater(pixel[2], 180)
        self.assertLess(pixel[0], 40)
        self.assertLess(pixel[1], 40)

    def test_two_grid_voices_have_both_colors_and_unattenuated_original_tones(self):
        self.make_asset("C4.mkv", ("red",), (400,))
        self.make_asset("D4.mkv", ("green",), (600,))
        data = song(note(60, 0, 0.8), note(62, 0.2, 0.6), duration=0.8)
        options = self.options("two.mp4")
        video = render_midi(data, options)
        preview = render_preview_audio(data, options, self.directory / "two.wav")
        self.assert_red(self.pixel(video, 0.3, 32, 64))
        self.assert_green(self.pixel(video, 0.3, 96, 64))
        # A visible divider distinguishes two identical-looking note clips too.
        self.assertTrue(all(value < 20 for value in self.pixel(video, 0.3, 64, 64)))
        before = self.amplitude(preview, 0.1, 400)
        during = self.amplitude(preview, 0.3, 400)
        after = self.amplitude(preview, 0.7, 400)
        self.assertGreater(before, 1500)
        self.assertAlmostEqual(during / before, 1.0, delta=0.02)
        self.assertAlmostEqual(after / before, 1.0, delta=0.02)
        self.assertGreater(self.amplitude(preview, 0.3, 600), 1500)
        self.assertLess(self.amplitude(preview, 0.7, 600), 80)
        for frequency in (400, 600):
            self.assertAlmostEqual(self.amplitude(video, 0.3, frequency) / self.amplitude(preview, 0.3, frequency), 1.0, delta=0.15)
        self.assertEqual(len(self.audio_samples(preview)), 38400)

    def test_five_simultaneous_notes_are_all_in_the_grid(self):
        for filename, color, frequency in (("C4.mkv", "red", 400), ("D4.mkv", "green", 600), ("E4.mkv", "blue", 800), ("F4.mkv", "yellow", 1000), ("G4.mkv", "magenta", 1200)):
            self.make_asset(filename, (color,), (frequency,))
        data = song(*(note(pitch, 0, 0.2) for pitch in (60, 62, 64, 65, 67)), duration=0.3)
        output = render_midi(data, RenderOptions(self.directory, self.directory / "five.mp4", width=192, height=128, fps=20))
        self.assert_red(self.pixel(output, 0.1, 32, 32))
        self.assert_green(self.pixel(output, 0.1, 96, 32))
        self.assert_blue(self.pixel(output, 0.1, 160, 32))
        yellow = self.pixel(output, 0.1, 32, 96)
        self.assertGreater(yellow[0], 180)
        self.assertGreater(yellow[1], 180)
        magenta = self.pixel(output, 0.1, 96, 96)
        self.assertGreater(magenta[0], 180)
        self.assertGreater(magenta[2], 180)
        self.assertTrue(all(value < 20 for value in self.pixel(output, 0.1, 160, 96)))

    def test_same_pitch_overlaps_keep_two_offsets_and_two_voices(self):
        self.make_asset("C4.mkv", ("red", "green", "blue"), (400, 800, 1200), duration=0.6)
        data = song(
            SimpleNamespace(pitch=60, start=0.0, end=0.4, source_offset=0.0),
            SimpleNamespace(pitch=60, start=0.1, end=0.4, source_offset=0.2),
            duration=0.4,
        )
        options = self.options("same pitch.mp4")
        output = render_midi(data, options)
        preview = render_preview_audio(data, options, self.directory / "same pitch.wav")
        self.assert_red(self.pixel(output, 0.15, 32, 64))
        self.assert_green(self.pixel(output, 0.15, 96, 64))
        self.assertGreater(self.amplitude(preview, 0.12, 400), 1500)
        self.assertGreater(self.amplitude(preview, 0.12, 800), 1500)

    def test_held_note_continues_when_other_voice_ends_and_grid_changes(self):
        self.make_asset("C4.mkv", ("red", "green", "blue", "yellow"), (400, 600, 800, 1000))
        self.make_asset("D4.mkv", ("magenta",), (1800,))
        baseline = song(note(60, 0, 0.8), duration=0.8)
        overlapped = song(note(60, 0, 0.8), note(62, 0.3, 0.5), duration=0.8)
        options = self.options("continued.mp4")
        video = render_midi(overlapped, options)
        plain_audio = render_preview_audio(baseline, options, self.directory / "baseline.wav")
        layered_audio = render_preview_audio(overlapped, options, self.directory / "overlapped.wav")
        self.assert_blue(self.pixel(video, 0.55, 64, 64))
        self.assertEqual(self.audio_samples(plain_audio, 0.55, 0.04), self.audio_samples(layered_audio, 0.55, 0.04))
        self.assertGreater(self.amplitude(layered_audio, 0.55, 800, 0.04), 1500)

    def test_time_selection_keeps_video_and_sound_source_offsets(self):
        from notes import MidiData, NoteEvent, TrackInfo
        from selection import select_midi

        self.make_asset("C4.mkv", ("red", "green", "blue"), (400, 800, 1200), duration=0.6)
        full = MidiData((NoteEvent(60, 0, 0.6, 100, 0, 0),), (TrackInfo(0, "note", (0,), 1),), 0.6, ())
        selected = select_midi(full, start=0.25, end=0.55)
        options = self.options("selected.mp4")
        video = render_midi(selected, options)
        preview = render_preview_audio(selected, options, self.directory / "selected.wav")
        self.assert_green(self.pixel(video, 0.0, 64, 64))
        self.assert_blue(self.pixel(video, 0.2, 64, 64))
        self.assertGreater(self.amplitude(preview, 0.02, 800), 1500)
        self.assertLess(self.amplitude(preview, 0.02, 400), 100)
        self.assertGreater(self.amplitude(preview, 0.22, 1200), 1500)
        self.assertEqual(len(self.audio_samples(preview)), 14400)

    def test_preview_limiter_compensates_lookahead_delay(self):
        self.make_asset("C4.mkv", duration=0.4, impulse=5000)
        preview = render_preview_audio(song(note(60, 0, 0.4), duration=0.4), self.options("unused.mp4"), self.directory / "impulse.wav")
        samples = self.audio_samples(preview)
        self.assertEqual(max(range(len(samples)), key=lambda index: abs(samples[index])), 5000)
        self.assertEqual(len(samples), 19200)

    def test_preview_batches_many_notes_without_losing_late_notes(self):
        self.make_asset("C4.mkv")
        data = song(*(note(60, index / 20, (index + 1) / 20) for index in range(30)), duration=1.5)
        progress = []
        preview = render_preview_audio(data, self.options("unused.mp4"), self.directory / "many.wav", lambda value, text: progress.append(value))
        self.assertGreater(self.amplitude(preview, 1.3, 400), 1500)
        self.assertEqual(len(self.audio_samples(preview)), 72000)
        self.assertTrue(all(a <= b + 1e-9 for a, b in zip(progress, progress[1:])))
        self.assertEqual(progress[-1], 1.0)

    def test_no_audio_track_is_reported_without_creating_preview(self):
        self.make_asset("C4.mkv", audio=False)
        destination = self.directory / "silent.wav"
        with self.assertRaisesRegex(RenderError, "没有可试听的音轨"):
            render_preview_audio(song(note(60, 0, 0.2)), self.options("unused.mp4"), destination)
        self.assertFalse(destination.exists())

    def test_cancelled_preview_preserves_existing_file(self):
        self.make_asset("C4.mkv")
        output = self.directory / "previous.wav"
        output.write_bytes(b"old preview")
        cancelled = threading.Event()

        def report(value, text):
            if value > 0:
                cancelled.set()

        with self.assertRaises(RenderCancelled):
            render_preview_audio(song(note(60, 0, 0.8), duration=0.8), self.options("unused.mp4", overwrite=True), output, report, cancelled)
        self.assertEqual(output.read_bytes(), b"old preview")
        self.assertFalse(list(self.directory.glob(".midi-stitch-preview-*")))

    def test_preview_cannot_replace_an_input_through_a_hard_link(self):
        source = self.make_asset("C4.mkv")
        output = self.directory / "input link.wav"
        try:
            os.link(source, output)
        except OSError:
            self.skipTest("Hard links are unavailable")
        previous = source.read_bytes()
        with self.assertRaisesRegex(RenderError, "不能覆盖"):
            render_preview_audio(song(note(60, 0, 0.2)), self.options("unused.mp4", overwrite=True), output)
        self.assertEqual(source.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
