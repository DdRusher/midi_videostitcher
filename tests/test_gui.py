"""Exercise the desktop workflow without leaving windows or encoding video.

MIDI parsing and Tk widgets are real; only the expensive video render and the
native message dialogs are substituted.  Headless machines skip these tests.
"""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

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

        def dialog(kind):
            def record(*args, **_kwargs):
                self.dialogs.append((kind, args))
                self.dialog_threads.append(threading.get_ident())
                return True
            return record

        self.patches = [
            patch.object(gui.messagebox, "showerror", side_effect=dialog("error")),
            patch.object(gui.messagebox, "showinfo", side_effect=dialog("info")),
            patch.object(gui.messagebox, "askyesno", return_value=True),
        ]
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = gui.StitcherApp()
        self.app.withdraw()
        self.app.update()
        self.app.video_dir.set(str(self.directory))
        self.app.output_path.set(str(self.directory / "result.mp4"))

    def tearDown(self):
        try:
            self.app._cancel.set()
            if self.app._busy:
                self._wait_for_idle()
            if self.app._thread is not None:
                self.app._thread.join(timeout=2)
                self.assertFalse(self.app._thread.is_alive(), "GUI worker was left running")
        finally:
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
            mido.Message("note_on", channel=1, note=first_pitch + 4, velocity=80, time=240),
            mido.Message("note_off", channel=1, note=first_pitch + 4, velocity=0, time=480),
        ]))
        midi.tracks.append(mido.MidiTrack([
            mido.MetaMessage("track_name", name="Bass", time=0),
            mido.Message("note_on", channel=2, note=first_pitch - 12, velocity=80, time=480),
            mido.Message("note_off", channel=2, note=first_pitch - 12, velocity=0, time=480),
        ]))
        path = self.directory / filename
        midi.save(path)
        return path

    def _wait_for_idle(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.app._busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.app.update()
        self.assertFalse(self.app._busy, "GUI did not consume the worker result")

    def _load(self, path):
        self.app.midi_path.set(str(path))
        self.app._read_midi()
        self._wait_for_idle()
        self.assertFalse([item for item in self.dialogs if item[0] == "error"])

    def _check_assets(self):
        data = self.app._selected_data()
        assets = {note.pitch: self.directory / f"{note.name}.mp4" for note in data.notes}
        with patch.object(gui, "validate_assets", return_value=assets), \
                patch.object(gui, "find_ffmpeg", return_value="ffmpeg"):
            self.app._check_assets()
            self._wait_for_idle()
        self.assertIn("检查通过", self.app.asset_summary.get())

    def _assert_controls_restored(self):
        self.assertEqual(str(self.app.midi_entry.cget("state")), "normal")
        self.assertEqual(str(self.app.track_box.cget("state")), "readonly")
        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")

    def test_real_midi_track_and_channel_selection(self):
        self._load(self.midi_a)
        self.assertEqual(self.app._track_indices, [1, 2])
        self.assertEqual({note.pitch for note in self.app._selected_data().notes}, {60, 64})

        self.app.channel_choice.set("通道 2")
        self.app.channel_box.event_generate("<<ComboboxSelected>>")
        self.app.update()
        selected = self.app._selected_data()
        self.assertEqual([note.pitch for note in selected.notes], [64])
        self.assertIn("E4", self.app.notes_text.get("1.0", "end"))

        self.app.track_box.current(1)
        self.app.channel_choice.set("全部通道")
        self.app.track_box.event_generate("<<ComboboxSelected>>")
        self.app.update()
        self.assertEqual([note.pitch for note in self.app._selected_data().notes], [48])
        self.app.channel_choice.set("通道 1")
        self.app.channel_box.event_generate("<<ComboboxSelected>>")
        self.app.update()
        with self.assertRaisesRegex(ValueError, "没有可导出的音符"):
            self.app._selected_data()

    def test_asset_success_does_not_survive_midi_or_selection_changes(self):
        self._load(self.midi_a)
        self._check_assets()

        self.app.midi_path.set(str(self.midi_b))
        self.assertNotIn("检查通过", self.app.asset_summary.get())
        self.assertIn("重新检查", self.app.asset_summary.get())
        self.app._read_midi()
        self._wait_for_idle()
        self.assertEqual({note.pitch for note in self.app._selected_data().notes}, {72, 76})
        self.assertNotIn("检查通过", self.app.asset_summary.get())

        self._check_assets()
        self.app._read_midi()  # Same-path rereads invalidate earlier checks too.
        self.assertNotIn("检查通过", self.app.asset_summary.get())
        self._wait_for_idle()
        self.assertNotIn("检查通过", self.app.asset_summary.get())

        self._check_assets()
        self.app.channel_choice.set("通道 2")
        self.app.channel_box.event_generate("<<ComboboxSelected>>")
        self.app.update()
        self.assertNotIn("检查通过", self.app.asset_summary.get())

    def test_export_worker_success_updates_ui_on_main_thread(self):
        self._load(self.midi_a)
        release = threading.Event()
        self.addCleanup(release.set)
        worker_threads = []
        output = self.directory / "result.mp4"

        def successful_render(data, options, on_progress, cancel_event):
            worker_threads.append(threading.get_ident())
            self.assertEqual({note.pitch for note in data.notes}, {60, 64})
            self.assertEqual(options.output_path, output)
            if not release.wait(timeout=3):
                raise RuntimeError("test renderer was not released")
            on_progress(0.5, "正在导出测试片段")
            output.write_bytes(b"completed video")
            return output

        with patch.object(gui, "render_midi", side_effect=successful_render):
            self.app._start_export()
            self.assertEqual(str(self.app.export_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.midi_entry.cget("state")), "disabled")
            self.assertEqual(str(self.app.cancel_button.cget("state")), "normal")
            release.set()
            self._wait_for_idle()

        self.assertEqual(output.read_bytes(), b"completed video")
        self.assertEqual(self.app.status.get(), "导出完成")
        self.assertEqual(self.app.percent.get(), "100%")
        self.assertEqual(str(self.app.open_video_button.cget("state")), "normal")
        self.assertNotEqual(worker_threads, [threading.get_ident()])
        self.assertTrue(self.dialog_threads)
        self.assertTrue(all(identifier == threading.get_ident() for identifier in self.dialog_threads))
        self._assert_controls_restored()

    def test_export_worker_error_restores_editable_controls(self):
        self._load(self.midi_a)
        with patch.object(gui, "render_midi", side_effect=RenderError("缺少素材：C4")):
            self.app._start_export()
            self._wait_for_idle()

        self.assertFalse((self.directory / "result.mp4").exists())
        self.assertTrue(any(kind == "error" and "缺少素材：C4" in args[1]
                            for kind, args in self.dialogs))
        self._assert_controls_restored()

    def test_export_cancellation_finishes_without_error_dialog_or_output(self):
        self._load(self.midi_a)
        entered = threading.Event()

        def cancellable_render(_data, _options, on_progress, cancel_event):
            entered.set()
            on_progress(0.25, "正在导出测试片段")
            if not cancel_event.wait(timeout=3):
                raise RuntimeError("cancellation was not delivered")
            raise RenderCancelled("任务已取消")

        with patch.object(gui, "render_midi", side_effect=cancellable_render):
            self.app._start_export()
            self.assertTrue(entered.wait(timeout=1))
            self.app._cancel_job()
            self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
            self._wait_for_idle()

        self.assertEqual(self.app.status.get(), "任务已取消")
        self.assertFalse((self.directory / "result.mp4").exists())
        self.assertFalse(self.dialogs, self.dialogs)
        self._assert_controls_restored()


if __name__ == "__main__":
    unittest.main()
