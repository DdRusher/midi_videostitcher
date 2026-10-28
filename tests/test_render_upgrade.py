"""Real-media regression checks for selected windows, dynamics, and music mode."""
from dataclasses import replace
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

from material_selection import MaterialSelection, plan_material_offsets
from notes import MidiData, NoteEvent, TrackInfo
import renderer
from renderer import (RenderOptions, RenderError, RenderCancelled, build_polyphonic_timeline,
                      inspect_materials, render_audio, render_midi, render_preview_audio)
from selection import select_midi
import test_renderer as legacy


def song(*notes, duration):
    return MidiData(tuple(notes), (TrackInfo(0, "notes", (0,), len(notes)),), duration, ())


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
class RenderUpgradeTests(unittest.TestCase):
    setUp = legacy.PolyphonicExportTests.setUp
    run_ffmpeg = legacy.PolyphonicExportTests.run_ffmpeg
    make_asset = legacy.PolyphonicExportTests.make_asset
    options = legacy.PolyphonicExportTests.options
    pixel = legacy.PolyphonicExportTests.pixel
    audio_samples = legacy.PolyphonicExportTests.audio_samples
    amplitude = legacy.PolyphonicExportTests.amplitude
    assert_red = legacy.PolyphonicExportTests.assert_red
    assert_green = legacy.PolyphonicExportTests.assert_green
    assert_blue = legacy.PolyphonicExportTests.assert_blue

    def test_velocity_controls_amplitude_and_can_be_disabled(self):
        self.make_asset("C4.mkv", duration=1.0)
        data = song(NoteEvent(60, 0, 0.3, 127, 0, 0), NoteEvent(60, 0.4, 0.7, 32, 0, 0), duration=0.8)
        options = self.options("unused.mp4")
        loud_soft = render_audio(data, options, self.directory / "dynamics.wav")
        unchanged = render_audio(data, replace(options, respect_velocity=False), self.directory / "same-level.wav")
        ratio = self.amplitude(loud_soft, .45, 400) / self.amplitude(loud_soft, .05, 400)
        self.assertAlmostEqual(ratio, 32 / 127, delta=.004)
        self.assertAlmostEqual(self.amplitude(unchanged, .45, 400) / self.amplitude(unchanged, .05, 400), 1, delta=.004)
        self.assertEqual([span.voices[0].velocity for span in build_polyphonic_timeline(data, 20) if span.voices], [127, 32])

    def test_fixed_window_loops_selected_video_and_audio_only(self):
        self.make_asset("C4.mkv", ("red", "green", "blue", "yellow"), (400, 600, 800, 1000), duration=2)
        data = song(NoteEvent(60, 0, 2, 127, 0, 0), duration=2)
        options = self.options("window.mp4", material_selections={60: MaterialSelection(.5, 1.5)})
        video = render_midi(data, options)
        preview = render_audio(data, options, self.directory / "window.wav")
        for instant, frequency, color in ((.1, 600, "green"), (.65, 800, "blue"), (1.1, 600, "green"), (1.65, 800, "blue")):
            with self.subTest(time=instant):
                getattr(self, "assert_" + color)(self.pixel(video, instant, 64, 64))
                self.assertGreater(self.amplitude(preview, instant, frequency), 2400)
                self.assertLess(self.amplitude(preview, instant, 400), 40)
                self.assertLess(self.amplitude(preview, instant, 1000), 40)

    def test_random_excerpt_repeats_for_preview_export_and_seed(self):
        self.make_asset("C4.mkv", ("red", "green", "blue", "yellow"), (400, 600, 800, 1000), duration=2)
        data = song(NoteEvent(60, 0, .3, 127, 0, 0), NoteEvent(60, .4, .7, 127, 0, 0), duration=.8)
        choices = {60: MaterialSelection(.5, 1.5, "random")}
        options = self.options("random.mp4", material_selections=choices, random_seed=173)
        one = render_audio(data, options, self.directory / "random-one.wav")
        two = render_preview_audio(data, options, self.directory / "random-two.wav")
        different = render_audio(data, replace(options, random_seed=174), self.directory / "different.wav")
        self.assertEqual(self.audio_samples(one), self.audio_samples(two))
        self.assertNotEqual(self.audio_samples(one), self.audio_samples(different))
        planned = plan_material_offsets(data, choices, {60: 2}, 173)
        video = render_midi(data, options)
        for note in planned.notes:
            source_time = .5 + note.source_offset + .05
            expected = 600 if source_time < 1 else 800
            self.assertGreater(self.amplitude(one, note.start + .05, expected), 2200)
            color = "green" if expected == 600 else "blue"
            getattr(self, "assert_" + color)(self.pixel(video, note.start + .05, 64, 64))

    def test_middle_midi_cut_continues_inside_source_window(self):
        self.make_asset("C4.mkv", ("red", "green", "blue", "yellow"), (400, 600, 800, 1000), duration=2)
        full = song(NoteEvent(60, 0, 2, 127, 0, 0), duration=2)
        data = select_midi(full, start=.75, end=1.25)
        options = self.options("middle.mp4", material_selections={60: MaterialSelection(.5, 1.5)})
        video = render_midi(data, options)
        audio = render_audio(data, options, self.directory / "middle.wav")
        self.assert_blue(self.pixel(video, .1, 64, 64))
        self.assert_green(self.pixel(video, .4, 64, 64))
        self.assertGreater(self.amplitude(audio, .1, 800), 2400)
        self.assertGreater(self.amplitude(audio, .4, 600), 2400)

    def test_imported_music_labels_and_audio_export_need_no_materials(self):
        self.make_asset("source.mkv", ("red", "blue"), (400, 900), duration=.8)
        music = self.directory / "source-source.wav"
        data = song(NoteEvent(60, 0, .3, 20, 0, 0), duration=.3)
        options = RenderOptions(self.directory / "absent", self.directory / "labels.mp4", width=320, height=180, fps=20,
                                video_mode="labels", audio_path=music, audio_start=.4)
        video = render_midi(data, options)
        audio = render_audio(data, replace(options, video_mode="clips"), self.directory / "imported.wav")
        self.assertGreater(self.amplitude(audio, .05, 900), 2400)
        self.assertLess(self.amplitude(audio, .05, 400), 50)
        self.assertAlmostEqual(self.amplitude(video, .05, 900) / self.amplitude(audio, .05, 900), 1, delta=.12)
        self.assertEqual(len(self.audio_samples(audio)), 14400)
        self.assertTrue(any(component > 20 for component in self.pixel(video, .1, 160, 90)))

    def test_wav_assets_are_supported_for_audio_only_and_labels(self):
        self.make_asset("temporary.mkv", duration=.4)
        source = self.directory / "temporary-source.wav"
        shutil.copyfile(source, self.directory / "C4.wav")
        data = song(NoteEvent(60, 0, .3, 127, 0, 0), duration=.3)
        options = self.options("labels.mp4", video_mode="labels")
        inspected = inspect_materials(data, self.directory)
        self.assertEqual(inspected[60].duration, .4)
        self.assertFalse(inspected[60].has_video)
        self.assertTrue(inspected[60].has_audio)
        audio = render_audio(data, options, self.directory / "wave.wav")
        self.assertGreater(self.amplitude(audio, .05, 400), 2400)
        render_midi(data, options)
        with self.assertRaisesRegex(RenderError, "缺少"):
            render_midi(data, replace(options, video_mode="clips", output_path=self.directory / "no-video.mp4"))

    def test_many_notes_decode_the_same_source_once(self):
        self.make_asset("C4.mkv", duration=.4)
        data = song(*(NoteEvent(60, index / 20, (index + 1) / 20, 127, 0, 0) for index in range(80)), duration=4)
        original = renderer._run_process
        commands = []
        def record(command, *args, **kwargs):
            commands.append(list(command))
            return original(command, *args, **kwargs)
        with patch("renderer._run_process", side_effect=record):
            output = render_audio(data, self.options("unused.mp4"), self.directory / "many.wav")
        source_decodes = [command for command in commands if any("source-audio-060.f32" in item for item in command)]
        self.assertEqual(len(source_decodes), 1)
        self.assertEqual(source_decodes[0].count("-i"), 1)
        self.assertEqual(len(self.audio_samples(output)), 4 * 48000)
        self.assertGreater(self.amplitude(output, 3.8, 400), 2400)

    def test_inspection_cache_updates_when_source_changes(self):
        self.make_asset("C4.mkv", duration=.4)
        data = song(NoteEvent(60, 0, .3, 127, 0, 0), duration=.3)
        first = inspect_materials(data, self.directory)[60]
        self.make_asset("C4.mkv", duration=.8)
        second = inspect_materials(data, self.directory)[60]
        self.assertEqual(first.duration, .4)
        self.assertEqual(second.duration, .8)

    def test_nonround_material_period_is_precise_without_ffprobe(self):
        # Thirteen frames at 30 fps are displayed as only 0.43 in ffmpeg -i.
        # The exact MP4 movie clock must drive both waveform and video looping.
        samples = np.round(3000 * np.sin(2 * np.pi * 400 * np.arange(20800) / 48000)).astype("<i2")
        source = self.directory / "thirteen.wav"
        with wave.open(str(source), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(np.repeat(samples[:, None], 2, axis=1).tobytes())
        asset = self.directory / "C4.mp4"
        self.run_ffmpeg(["-f", "lavfi", "-i", "color=red:s=64x64:r=30", "-i", str(source), "-frames:v", "13", "-t", "0.433333333",
                         "-c:v", "libx264", "-bf", "0", "-c:a", "aac", str(asset)])
        data = song(NoteEvent(60, 0, 2.4, 127, 0, 0), duration=2.4)
        original_which = renderer.shutil.which
        with patch("renderer.shutil.which", side_effect=lambda name: None if name == "ffprobe" else original_which(name)):
            info = inspect_materials(data, self.directory)[60]
        self.assertGreater(info.duration, .433)
        self.assertLess(info.duration, .435)
        options = self.options("unused.mp4")
        rendered = render_audio(data, options, self.directory / "loop.wav")
        period = round(info.duration * 48000)
        raw = self.run_ffmpeg(["-i", str(asset), "-af", "aresample=48000:async=1:first_pts=0", "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"])
        reference = np.frombuffer(raw, dtype="<i2").astype(np.int32)
        reference = np.pad(reference[:period], (0, max(0, period-len(reference))))
        expected = np.resize(reference, round(2.4 * 48000))
        actual = np.array(self.audio_samples(rendered), dtype=np.int32)
        self.assertLessEqual(np.max(np.abs(actual-expected)), 1)

    def test_nonround_selected_window_retains_exact_video_period(self):
        self.make_asset("C4.mkv", ("red", "green", "blue", "yellow"), (400, 600, 800, 1000), duration=2)
        data = song(NoteEvent(60, 0, 4, 127, 0, 0), duration=4)
        options = self.options("unused.mp4", material_selections={60: MaterialSelection(.13, 1.36)})
        context = renderer._prepare(data, options, None, None)
        work = self.directory / "cached-window"
        work.mkdir()
        video_context = renderer._prepare_video_windows(context, options, work, None, lambda *args: None)
        info = renderer._probe_info(context.ffmpeg, video_context.assets[60], None)
        self.assertAlmostEqual(info.duration, 1.23, delta=.00003)
        self.assertAlmostEqual(video_context.durations[60], 1.23)
        self.assertTrue(info.has_video)
        self.assertFalse(info.has_audio)

    def test_invalid_dynamics_and_options_fail_before_output(self):
        data = song(NoteEvent(60, 0, .3, 0, 0, 0), duration=.3)
        with self.assertRaisesRegex(RenderError, "力度"):
            render_audio(data, self.options("unused.mp4"), self.directory / "bad.wav")
        valid = song(NoteEvent(60, 0, .3, 127, 0, 0), duration=.3)
        for changes in ({"audio_start":-1}, {"render_workers":-1}, {"video_mode":"unknown"}):
            with self.subTest(changes=changes), self.assertRaises(RenderError):
                render_midi(valid, replace(self.options("bad.mp4"), **changes))
        self.assertFalse((self.directory / "bad.mp4").exists())


class ParallelCancellationTests(unittest.TestCase):
    def test_one_worker_failure_stops_other_workers_and_retains_error(self):
        segments = (renderer.PolyphonicSegment(0, 2, ()), renderer.PolyphonicSegment(2, 4, ()))
        context = SimpleNamespace(segments=segments)
        options = RenderOptions(".", "unused.mp4", width=64, height=64, fps=20, render_workers=2)
        stopped = threading.Event()
        def encode(context, options, segment, destination, script, cancel, progress):
            if segment.start_frame == 0:
                time.sleep(.05)
                raise RenderError("source decoder failed")
            while not cancel.is_set():
                time.sleep(.01)
            stopped.set()
            renderer._check_cancel(cancel)
        started = time.monotonic()
        with patch("renderer._encode_grid_segment", side_effect=encode):
            with self.assertRaisesRegex(RenderError, "source decoder failed"):
                renderer._render_clip_segments(context, options, Path("."), None, None)
        self.assertTrue(stopped.is_set())
        self.assertLess(time.monotonic()-started, 1)


if __name__ == "__main__":
    unittest.main()
