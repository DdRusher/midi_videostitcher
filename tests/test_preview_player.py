"""Test preview controls without making sound on the test machine."""

from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import preview_player as playback


class PreviewPlayerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.audio = self.directory / "试听 原声.wav"
        with wave.open(str(self.audio), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(bytes(4 * 12000))
        self.sound = SimpleNamespace(
            PlaySound=Mock(), SND_FILENAME=1, SND_ASYNC=2, SND_NODEFAULT=4,
        )
        self.addCleanup(patch.stopall)

    def windows(self):
        patch.object(playback, "_WINDOWS", True).start()
        patch.object(playback, "_winsound", self.sound).start()
        return playback.PreviewPlayer()

    def test_creation_does_not_start_sound(self):
        player = self.windows()
        self.assertFalse(player.is_playing())
        self.sound.PlaySound.assert_not_called()

    def test_play_is_nonblocking_and_stop_releases_filename(self):
        player = self.windows()
        with patch.object(playback.time, "monotonic", return_value=100):
            player.play(self.audio)
            self.assertTrue(player.is_playing())
        self.assertAlmostEqual(player.duration, 0.25)
        self.sound.PlaySound.assert_called_once_with(str(self.audio.resolve()), 7)
        player.stop()
        self.sound.PlaySound.assert_called_with(None, 0)
        self.assertFalse(player.is_playing())

    def test_completion_is_reported_and_releases_audio(self):
        player = self.windows()
        with patch.object(playback.time, "monotonic", return_value=100):
            player.play(self.audio)
        with patch.object(playback.time, "monotonic", return_value=100.3):
            self.assertFalse(player.is_playing())
        self.sound.PlaySound.assert_called_with(None, 0)

    def test_new_preview_stops_previous_preview(self):
        player = self.windows()
        player.play(self.audio)
        player.play(self.audio)
        self.assertEqual(self.sound.PlaySound.call_count, 3)
        self.assertEqual(self.sound.PlaySound.call_args_list[1].args, (None, 0))
        player.stop()

    def test_disconnected_device_during_completion_does_not_break_controls(self):
        player = self.windows()
        with patch.object(playback.time, "monotonic", return_value=100):
            player.play(self.audio)
        self.sound.PlaySound.side_effect = RuntimeError("device disconnected")
        with patch.object(playback.time, "monotonic", return_value=100.3):
            self.assertFalse(player.is_playing())
        player.stop()
        self.assertFalse(player.is_playing())
        self.sound.PlaySound.side_effect = None
        player.play(self.audio)
        player.stop()

    def test_missing_invalid_or_empty_audio_is_clear(self):
        player = self.windows()
        with self.assertRaisesRegex(playback.PreviewPlaybackError, "不存在"):
            player.play(self.directory / "missing.wav")
        invalid = self.directory / "bad.wav"
        invalid.write_bytes(b"not a WAVE")
        with self.assertRaisesRegex(playback.PreviewPlaybackError, "PCM WAV"):
            player.play(invalid)
        with wave.open(str(invalid), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
        with self.assertRaisesRegex(playback.PreviewPlaybackError, "没有声音时长"):
            player.play(invalid)
        self.sound.PlaySound.assert_not_called()

    def test_sound_device_error_is_actionable(self):
        player = self.windows()
        self.sound.PlaySound.side_effect = RuntimeError("device unavailable")
        with self.assertRaisesRegex(playback.PreviewPlaybackError, "声音输出设备"):
            player.play(self.audio)
        self.assertFalse(player.is_playing())

    def test_missing_platform_player_is_explained(self):
        with patch.object(playback, "_WINDOWS", False), patch.object(playback.shutil, "which", return_value=None):
            player = playback.PreviewPlayer()
            with self.assertRaisesRegex(playback.PreviewPlaybackError, "没有可用"):
                player.play(self.audio)

    def test_process_playback_can_be_stopped(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("ffplay", 0.05)
        process.communicate.return_value = (b"", b"")
        with patch.object(playback, "_WINDOWS", False), patch.object(playback.shutil, "which", side_effect=lambda name: "/bin/ffplay" if name == "ffplay" else None), patch.object(playback.subprocess, "Popen", return_value=process):
            player = playback.PreviewPlayer()
            player.play(self.audio)
            self.assertTrue(player.is_playing())
            player.stop()
        process.terminate.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=1)
        self.assertFalse(player.is_playing())


if __name__ == "__main__":
    unittest.main()
