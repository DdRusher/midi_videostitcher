"""Card videos retain frame-level note timing with a single FFmpeg encode."""

from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from label_video import encode_label_video
from notes import MidiData, NoteEvent
from renderer import PolyphonicSegment, TimelineVoice, find_ffmpeg


class CardVideoTests(unittest.TestCase):
    def test_single_frame_notes_and_rests_keep_exact_positions(self):
        ffmpeg = find_ffmpeg()
        for fps in (24, 30, 60, 120):
            with self.subTest(fps=fps), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                data = MidiData((NoteEvent(60, 1/fps, 3/fps, 64, 0, 0),), (), 8/fps, ())
                voice = TimelineVoice(0, 60, 0)
                segments = (PolyphonicSegment(0, 1, ()), PolyphonicSegment(1, 3, (voice,)),
                            PolyphonicSegment(3, 5, ()), PolyphonicSegment(5, 6, (voice,)),
                            PolyphonicSegment(6, 8, ()))
                options = SimpleNamespace(width=160, height=96, fps=fps, respect_velocity=True)
                output = directory / "cards.mkv"
                encode_label_video(data, segments, options, output, directory, ffmpeg)
                result = subprocess.run([ffmpeg, "-v", "error", "-i", str(output), "-f", "rawvideo",
                                         "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True)
                size = 160 * 96 * 3
                frames = [result.stdout[start:start+size] for start in range(0, len(result.stdout), size)]
                self.assertEqual(len(frames), 8)
                for index, frame in enumerate(frames):
                    self.assertEqual(sum(frame) / size > 5, index in (1, 2, 5), f"frame {index} at {fps} fps")

    def test_cancel_before_creating_cards(self):
        import threading
        from renderer import RenderCancelled

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(RenderCancelled):
                encode_label_video(SimpleNamespace(notes=[]), (PolyphonicSegment(0, 2, ()),),
                                   SimpleNamespace(fps=30), directory / "result.mkv", directory,
                                   "not-started", cancel)
            self.assertFalse((directory / "result.mkv").exists())


if __name__ == "__main__":
    unittest.main()
