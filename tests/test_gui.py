"""Real Tk workflow tests; media processing and audio playback are isolated."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import mido

try:
    import tkinter as tk
except ImportError:
    tk = None

if tk is not None:
    import gui
    from renderer import RenderCancelled, RenderError
else:
    gui = None


class FakePreviewPlayer:
    """No sound is emitted while testing playback ownership and invalidation."""

    def __init__(self):
        self.playing = False
        self.paths = []
        self.play_threads = []
        self.stop_count = 0
        self.playback_error = None

    def play(self, path):
        if not Path(path).is_file():
            raise ValueError("试听文件不存在")
        self.paths.append(Path(path))
        self.play_threads.append(threading.get_ident())
        self.playing = True

    def stop(self):
        self.stop_count += 1
        self.playing = False

    def is_playing(self):
        if self.playback_error:
            raise self.playback_error
        return self.playing


@unittest.skipIf(gui is None, "Tkinter is not installed")
class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        probe = None
        try:
            probe = tk.Tk()
            probe.withdraw()
            probe.update()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display is not available: {error}") from error
        finally:
            if probe is not None:
                probe.destroy()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.midi_a = self._make_midi("a.mid", 60)
        self.midi_b = self._make_midi("b.mid", 72)
        self.dialogs = []
        self.dialog_threads = []
        self.player = FakePreviewPlayer()

        def dialog(kind):
            def record(*args, **_kwargs):
                self.dialogs.append((kind, args))
                self.dialog_threads.append(threading.get_ident())
                return True
            return record

        replacements = [
            patch.object(gui.messagebox, "showerror", side_effect=dialog("error")),
            patch.object(gui.messagebox, "showinfo", side_effect=dialog("info")),
            patch.object(gui.messagebox, "askyesno", return_value=True),
            patch.object(gui, "PreviewPlayer", return_value=self.player),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = gui.StitcherApp()
        self.app.withdraw()
        self.app.update()
        self.app.video_dir.set(str(self.directory))
        self.app.output_path.set(str(self.directory / "result.mp4"))

    def tearDown(self):
        try:
            if self.app is not None:
                self.app._cancel.set()
                if self.app._busy:
                    self._wait_for_idle()
                if self.app._thread is not None:
                    self.app._thread.join(timeout=2)
                    self.assertFalse(self.app._thread.is_alive(), "GUI worker was left running")
        finally:
            if self.app is not None:
                self.app.destroy()
            self.temporary.cleanup()

    def _make_midi(self, filename, first_pitch):
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("set_tempo", tempo=500000, time=0),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Melody", time=0),
            mido.Message("note_on", channel=0, note=first_pitch, velocity=80, time=0),
            mido.Message("note_off", channel=0, note=first_pitch, velocity=0, time=240),
            mido.Message("note_on", channel=1, note=first_pitch+4, velocity=80, time=240),
            mido.Message("note_off", channel=1, note=first_pitch+4, velocity=0, time=480),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Bass", time=0),
            mido.Message("note_on", channel=2, note=first_pitch-12, velocity=80, time=480),
            mido.Message("note_off", channel=2, note=first_pitch-12, velocity=0, time=480),
        ]))
        path = self.directory / filename
        midi.save(path)
        return path

    def _wait_for_idle(self, timeout=5):
        deadline = time.monotonic()+timeout
        while self.app._busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.app.update()
        self.assertFalse(self.app._busy, "GUI did not consume the worker result")

    def _load(self, path):
        self.app.midi_path.set(str(path))
        self.app._read_midi()
        self._wait_for_idle()
        self.assertFalse([item for item in self.dialogs if item[0] == "error"])

    def _make_assets(self):
        data = self.app._selected_data()
        assets = {}
        for note in data.notes:
            path = self.directory / f"{note.name}.mp4"
            if not path.exists():
                path.write_bytes(b"video fixture")
            assets[note.pitch] = path
        return assets

    def _check_assets(self):
        assets = self._make_assets()
        with patch.object(gui, "validate_assets", return_value=assets), \
                patch.object(gui, "find_ffmpeg", return_value="ffmpeg"):
            self.app._check_assets()
            self._wait_for_idle()
        self.assertIn("检查通过", self.app.asset_summary.get())

    def _assert_controls_restored(self):
        self.assertEqual(str(self.app.midi_entry.cget("state")), "normal")
        self.assertEqual(str(self.app.track_list.cget("state")), "normal")
        self.assertEqual(str(self.app.channel_box.cget("state")), "readonly")
        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")

    def _write_preview(self, data, options, path, on_progress=None, cancel_event=None):
        self.assertTrue(options.use_clip_audio)
        self.assertEqual(options.chord_policy, "all")
        self.assertNotEqual(threading.get_ident(), self._main_thread)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0"*round(8000*data.duration))
        if on_progress:
            on_progress(1, "试听已生成")
        return Path(path)

    def test_real_midi_multi_track_channel_and_time_selection(self):
        self._load(self.midi_a)
        self.assertEqual(self.app._track_indices, [1, 2])
        self.assertEqual(self.app.track_list.curselection(), (0, 1))
        self.assertEqual({note.pitch for note in self.app._selected_data().notes}, {48, 60, 64})
        self.assertFalse(self.player.paths, "loading a MIDI must not autoplay")

        self.app.channel_choice.set("通道 2")
        self.app.channel_box.event_generate("<<ComboboxSelected>>")
        self.app.update()
        self.assertEqual([note.pitch for note in self.app._selected_data().notes], [64])

        self.app.channel_choice.set("全部通道")
        self.app.track_list.selection_clear(0, "end")
        self.app.track_list.selection_set(1)
        self.app.track_list.event_generate("<<ListboxSelect>>")
        self.app.update()
        self.assertEqual([note.pitch for note in self.app._selected_data().notes], [48])
        self.app._select_all_tracks()
        self.app.start_time.set("00:00.600")
        self.app.end_time.set("0.9")
        selected = self.app._selected_data()
        self.assertAlmostEqual(selected.duration, .3)
        self.assertEqual({note.pitch for note in selected.notes}, {48, 64})
        for note in selected.notes:
            self.assertAlmostEqual(note.start, 0)
            self.assertAlmostEqual(note.end, .3)
            self.assertAlmostEqual(note.source_offset, .1)
        self.assertNotIn("C4", self.app.notes_text.get("1.0", "end"))

        self.app._clear_tracks()
        with self.assertRaisesRegex(ValueError, "至少选择一个轨道"):
            self.app._selected_data()
        self.app._select_all_tracks()
        self.app._reset_range()
        self.assertAlmostEqual(self.app._selected_data().duration, 1)

    def test_timeline_drag_updates_the_same_selection_used_for_export(self):
        self._load(self.midi_a)
        with patch.object(self.app.timeline, "winfo_width", return_value=800):
            self.app._timeline_press(SimpleNamespace(x=13))
            self.app._timeline_release(SimpleNamespace(x=13+774*.25))
            self.app._timeline_press(SimpleNamespace(x=787))
            self.app._timeline_release(SimpleNamespace(x=13+774*.75))
        self.assertAlmostEqual(gui.parse_time(self.app.start_time.get()), .25)
        self.assertAlmostEqual(gui.parse_time(self.app.end_time.get()), .75)
        self.assertAlmostEqual(self.app._selected_data().duration, .5)

    def test_dragging_to_song_end_does_not_round_past_fractional_duration(self):
        midi = mido.MidiFile(ticks_per_beat=5000)
        midi.tracks.append(mido.MidiTrack([
            mido.Message("note_on", note=60, velocity=80, time=0),
            mido.Message("note_off", note=60, velocity=0, time=9998),
        ]))
        path = self.directory/"fractional.mid"
        midi.save(path)
        self._load(path)
        with patch.object(self.app.timeline, "winfo_width", return_value=800):
            self.app._timeline_press(SimpleNamespace(x=787))
            self.app._timeline_release(SimpleNamespace(x=787))
        self.assertEqual(self.app.end_time.get(), "")
        self.assertAlmostEqual(self.app._selected_data().duration, .9998)

    def test_asset_success_does_not_survive_midi_or_selection_changes(self):
        self._load(self.midi_a)
        self._check_assets()
        self.app.midi_path.set(str(self.midi_b))
        self.assertNotIn("检查通过", self.app.asset_summary.get())
        self.assertIn("重新检查", self.app.asset_summary.get())
        self.app._read_midi()
        self._wait_for_idle()
        self.assertEqual({note.pitch for note in self.app._selected_data().notes}, {60, 72, 76})
        self.assertNotIn("检查通过", self.app.asset_summary.get())
        self._check_assets()
        self.app._read_midi()
        self.assertNotIn("检查通过", self.app.asset_summary.get())
        self._wait_for_idle()
        self.assertNotIn("检查通过", self.app.asset_summary.get())
        self._check_assets()
        self.app.start_time.set(".6")
        self.assertNotIn("检查通过", self.app.asset_summary.get())

    def test_export_success_runs_worker_and_restores_controls(self):
        self._load(self.midi_a)
        release = threading.Event()
        self.addCleanup(release.set)
        output = self.directory/"result.mp4"
        workers = []
        self.player.playing = True
        self.app._preview_active = True

        def success(data, options, on_progress, cancel_event):
            workers.append(threading.get_ident())
            self.assertEqual({note.pitch for note in data.notes}, {48, 60, 64})
            self.assertTrue(options.use_clip_audio)
            self.assertEqual(options.chord_policy, "all")
            self.assertFalse(self.player.playing, "export must stop audio playback first")
            if not release.wait(timeout=3):
                raise RuntimeError("test renderer was not released")
            on_progress(.5, "正在导出测试片段")
            output.write_bytes(b"completed video")
            return output

        with patch.object(gui, "render_midi", side_effect=success):
            self.app._start_export()
            self.assertEqual(str(self.app.track_list.cget("state")), "disabled")
            self.assertEqual(str(self.app.midi_entry.cget("state")), "disabled")
            self.assertEqual(str(self.app.cancel_button.cget("state")), "normal")
            release.set()
            self._wait_for_idle()
        self.assertEqual(output.read_bytes(), b"completed video")
        self.assertEqual(self.app.status.get(), "导出完成")
        self.assertEqual(self.app.percent.get(), "100%")
        self.assertEqual(str(self.app.open_video_button.cget("state")), "normal")
        self.assertEqual(len(workers), 1)
        self.assertNotEqual(workers[0], threading.get_ident())
        self.assertTrue(all(identifier == threading.get_ident() for identifier in self.dialog_threads))
        self._assert_controls_restored()

    def test_existing_output_is_untouched_when_overwrite_is_declined(self):
        self._load(self.midi_a)
        output = self.directory/"result.mp4"
        output.write_bytes(b"original")
        with patch.object(gui.messagebox, "askyesno", return_value=False), \
                patch.object(gui, "render_midi") as render:
            self.app._start_export()
            self.assertFalse(self.app._busy)
            render.assert_not_called()
        self.assertEqual(output.read_bytes(), b"original")

    def test_export_worker_error_restores_editable_controls(self):
        self._load(self.midi_a)
        with patch.object(gui, "render_midi", side_effect=RenderError("缺少素材：C4")):
            self.app._start_export()
            self._wait_for_idle()
        self.assertFalse((self.directory/"result.mp4").exists())
        self.assertTrue(any(kind == "error" and "缺少素材：C4" in args[1]
                            for kind, args in self.dialogs))
        self._assert_controls_restored()

    def test_export_cancellation_restores_controls_without_output(self):
        self._load(self.midi_a)
        entered = threading.Event()

        def cancellation(_data, _options, on_progress, cancel_event):
            entered.set()
            on_progress(.25, "正在导出测试片段")
            if not cancel_event.wait(timeout=3):
                raise RuntimeError("cancellation was not delivered")
            raise RenderCancelled("任务已取消")

        with patch.object(gui, "render_midi", side_effect=cancellation):
            self.app._start_export()
            self.assertTrue(entered.wait(timeout=1))
            self.app._cancel_job()
            self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
            self._wait_for_idle()
        self.assertEqual(self.app.status.get(), "任务已取消")
        self.assertFalse((self.directory/"result.mp4").exists())
        self.assertFalse(self.dialogs)
        self._assert_controls_restored()

    def test_preview_uses_excerpt_reuses_cache_and_stops_when_range_changes(self):
        self._load(self.midi_a)
        self.app.start_time.set(".6")
        self.app.end_time.set(".9")
        self._make_assets()
        self._main_thread = threading.get_ident()
        with patch.object(gui, "render_preview_audio", side_effect=self._write_preview) as render:
            self.app._start_preview()
            self._wait_for_idle()
            path = self.app._preview_path
            self.assertTrue(path.is_file())
            self.assertTrue(self.player.playing)
            self.assertEqual(str(self.app.stop_preview_button.cget("state")), "normal")
            self.assertAlmostEqual(render.call_args.args[0].duration, .3)
            self.assertTrue(all(identifier == self._main_thread for identifier in self.player.play_threads))
            self.app._stop_preview()
            self.app._start_preview()
            self._wait_for_idle()
            self.assertEqual(render.call_count, 1, "unchanged assets and selection should reuse WAV")
            self.assertEqual(len(self.player.paths), 2)
            self.app.start_time.set(".7")
            self.assertFalse(self.player.playing)
            self.assertIsNone(self.app._preview_path)
            self.assertIsNone(self.app._preview_cache_key)
            self.assertFalse(path.exists())
        self._assert_controls_restored()

    def test_cancelled_preview_never_autoplays_and_cleans_its_target(self):
        self._load(self.midi_a)
        self._make_assets()
        entered = threading.Event()

        def cancellation(_data, _options, path, on_progress, cancel_event):
            entered.set()
            Path(path).write_bytes(b"partial")
            if not cancel_event.wait(timeout=3):
                raise RuntimeError("preview cancellation was not delivered")
            raise RenderCancelled("任务已取消")

        with patch.object(gui, "render_preview_audio", side_effect=cancellation):
            self.app._start_preview()
            self.assertTrue(entered.wait(timeout=1))
            self.app._cancel_job()
            self._wait_for_idle()
        self.assertFalse(self.player.paths)
        self.assertFalse(list(Path(self.app._preview_temp.name).glob("*.wav")))
        self.assertEqual(self.app.status.get(), "任务已取消")
        self.assertFalse(self.dialogs)
        self._assert_controls_restored()

    def test_playback_error_is_reported_once_and_does_not_repeat_on_poll(self):
        self._load(self.midi_a)
        self._make_assets()
        self._main_thread = threading.get_ident()
        self.player.playback_error = ValueError("声音设备不可用")
        with patch.object(gui, "render_preview_audio", side_effect=self._write_preview):
            self.app._start_preview()
            self._wait_for_idle()
        deadline = time.monotonic()+.25
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertFalse(self.app._preview_active)
        self.assertFalse(self.player.playing)
        errors = [args for kind, args in self.dialogs if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("声音设备不可用", errors[0][1])
        self._assert_controls_restored()

    def test_close_stops_preview_before_removing_temporary_files(self):
        self._load(self.midi_a)
        self._make_assets()
        self._main_thread = threading.get_ident()
        with patch.object(gui, "render_preview_audio", side_effect=self._write_preview):
            self.app._start_preview()
            self._wait_for_idle()
        preview_directory = Path(self.app._preview_temp.name)
        self.assertTrue(self.player.playing)
        self.app._on_close()
        self.app = None
        self.assertFalse(self.player.playing)
        self.assertFalse(preview_directory.exists())


if __name__ == "__main__":
    unittest.main()
