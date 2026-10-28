"""Exercise desktop and CLI user flows with real MIDI files, without a GUI."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import mido

import main as app


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.midi_path = self.directory / "多轨测试.mid"
        self.videos = self.directory / "视频素材"
        self.videos.mkdir()
        self.write_midi()

    def write_midi(self):
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Conductor"),
            mido.MetaMessage("set_tempo", tempo=500_000),
            mido.MetaMessage("end_of_track", time=960),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Melody"),
            mido.Message("note_on", note=60, channel=0, velocity=90),
            mido.Message("note_on", note=62, channel=1, velocity=80, time=96),
            mido.Message("note_off", note=60, channel=0, time=144),
            mido.Message("note_off", note=62, channel=1, time=240),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Bass"),
            mido.Message("note_on", note=48, channel=2, velocity=90),
            mido.Message("note_off", note=48, channel=2, time=480),
        ]))
        midi.save(self.midi_path)

    def add_materials(self, *names):
        # --check validates filenames rather than decoding media, so empty
        # placeholders keep these MIDI/command-line tests independent of FFmpeg.
        for name in names:
            (self.videos / name).touch()

    def run_command(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = app.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def check(self, *extra):
        return self.run_command(
            "--midi", str(self.midi_path),
            "--video-dir", str(self.videos),
            "--check", *extra,
        )

    def test_default_track_skips_empty_conductor_and_selects_first_melody(self):
        self.add_materials("C4.mp4", "D4.mp4")
        code, output, error = self.check()
        self.assertEqual(code, 0, error)
        self.assertIn("C4 →", output)
        self.assertIn("D4 →", output)
        self.assertNotIn("C3 →", output)
        self.assertIn("2 个音符", output)
        self.assertIn("1.000 秒", output)
        self.assertEqual(error, "")

    def test_channel_numbers_are_one_based_for_users(self):
        self.add_materials("D4.mp4")
        code, output, error = self.check("--channel", "2")
        self.assertEqual(code, 0, error)
        self.assertIn("D4 →", output)
        self.assertNotIn("C4 →", output)
        self.assertIn("1 个音符", output)
        self.assertEqual(error, "")

    def test_user_channel_one_maps_to_midi_channel_zero(self):
        self.add_materials("C4.mp4")
        code, output, error = self.check("--channel", "1")
        self.assertEqual(code, 0, error)
        self.assertIn("C4 →", output)
        self.assertNotIn("D4 →", output)

    def test_user_channel_sixteen_is_supported(self):
        midi = mido.MidiFile(type=0, ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([
            mido.Message("note_on", note=64, channel=15, velocity=90),
            mido.Message("note_off", note=64, channel=15, time=480),
        ]))
        midi.save(self.midi_path)
        self.add_materials("E4.mp4")
        code, output, error = self.check("--channel", "16")
        self.assertEqual(code, 0, error)
        self.assertIn("E4 →", output)

    def test_list_tracks_needs_no_video_assets(self):
        code, output, error = self.run_command(
            "--midi", str(self.midi_path),
            "--video-dir", str(self.directory / "不存在的素材"),
            "--list-tracks",
        )
        self.assertEqual(code, 0, error)
        self.assertIn("[0] Conductor", output)
        self.assertIn("[1] Melody", output)
        self.assertIn("[2] Bass", output)
        self.assertIn("通道 1, 2", output)
        self.assertIn("通道 3", output)
        self.assertIn("1.000 秒", output)
        self.assertEqual(error, "")

    def test_missing_material_reports_the_missing_pitch_without_a_traceback(self):
        self.add_materials("C4.mp4")
        code, output, error = self.check()
        self.assertEqual(code, 1)
        self.assertIn("缺少", error)
        self.assertIn("D4", error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn("检查完成", output)

    def test_all_tracks_checks_bass_as_well_as_melody(self):
        self.add_materials("C4.mp4", "D4.mp4")
        code, _, error = self.check("--all-tracks")
        self.assertEqual(code, 1)
        self.assertIn("C3", error)
        self.add_materials("C3.mp4")
        code, output, error = self.check("--all-tracks")
        self.assertEqual(code, 0, error)
        for name in ("C3", "C4", "D4"):
            self.assertIn(f"{name} →", output)
        self.assertIn("3 个音符", output)

    def test_explicit_track_selects_only_that_track(self):
        self.add_materials("C3.mp4")
        code, output, error = self.check("--track", "2")
        self.assertEqual(code, 0, error)
        self.assertIn("C3 →", output)
        self.assertNotIn("C4 →", output)

    def test_empty_channel_selection_is_actionable(self):
        self.add_materials("C4.mp4", "D4.mp4")
        code, _, error = self.check("--channel", "3")
        self.assertEqual(code, 1)
        self.assertIn("没有可用音符", error)
        self.assertNotIn("Traceback", error)

    def test_invalid_sizes_and_channels_exit_through_argparse(self):
        invalid = [
            ("--size", "2x2", "宽高"),
            ("--size", "17x720", "宽高"),
            ("--size", "8000x720", "宽高"),
            ("--size", "1280", "分辨率"),
            ("--size", "1280x720x2", "分辨率"),
            ("--channel", "0", "通道"),
            ("--channel", "17", "通道"),
            ("--channel", "-1", "通道"),
            ("--channel", "x", "通道"),
        ]
        for flag, value, explanation in invalid:
            with self.subTest(flag=flag, value=value):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    app.main(["--midi", str(self.midi_path), "--check", flag, value])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn(explanation, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_track_and_all_tracks_are_mutually_exclusive(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            app.main(["--midi", str(self.midi_path), "--track", "1", "--all-tracks", "--check"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--all-tracks", stderr.getvalue())

    def test_bad_midi_returns_one_with_a_readable_error(self):
        self.midi_path.write_bytes(b"not MIDI")
        code, output, error = self.check()
        self.assertEqual(code, 1)
        self.assertIn("无法读取 MIDI", error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn("检查完成", output)

    def test_legacy_f_option_still_selects_midi(self):
        self.add_materials("C4.mp4", "D4.mp4")
        code, output, error = self.run_command(
            "--f", str(self.midi_path), "--video-dir", str(self.videos), "--check"
        )
        self.assertEqual(code, 0, error)
        self.assertIn("检查完成", output)

    def test_no_arguments_and_gui_option_launch_without_initializing_tk(self):
        for arguments in ((), ("--gui",)):
            with self.subTest(arguments=arguments):
                fake_gui = ModuleType("gui")
                fake_gui.launch_gui = Mock()
                with patch.dict("sys.modules", {"gui": fake_gui}):
                    code, output, error = self.run_command(*arguments)
                self.assertEqual(code, 0, error)
                fake_gui.launch_gui.assert_called_once_with()
                self.assertEqual(output, "")
                self.assertEqual(error, "")

    def test_demo_opens_gui_with_generated_inputs_without_running_tk(self):
        fake_gui = ModuleType("gui")
        fake_gui.launch_gui = Mock()
        fake_demo = ModuleType("examples.create_demo")
        fake_demo.create_demo = Mock(return_value=(self.midi_path, self.videos))
        with patch.dict("sys.modules", {"gui": fake_gui, "examples.create_demo": fake_demo}):
            code, _, error = self.run_command("--demo")
        self.assertEqual(code, 0, error)
        fake_demo.create_demo.assert_called_once_with()
        fake_gui.launch_gui.assert_called_once_with(
            midi_path=self.midi_path,
            video_dir=self.videos,
            output_path=self.midi_path.parent / "output.mp4",
        )


if __name__ == "__main__":
    unittest.main()
