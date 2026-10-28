"""Generated MIDI fixtures exercise timing and malformed note handling."""

from pathlib import Path
import tempfile
import unittest

import mido

from notes import load_midi, midi_note_to_durations, note_name, time_in_s


class MidiParsingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "测试 MIDI.mid"

    def tearDown(self):
        self.directory.cleanup()

    def save(self, *tracks, midi_type=1, ticks_per_beat=480):
        midi = mido.MidiFile(type=midi_type, ticks_per_beat=ticks_per_beat)
        for messages in tracks:
            midi.tracks.append(mido.MidiTrack(messages))
        midi.save(self.path)
        return self.path

    def test_default_tempo_keeps_intro_rests_and_trailing_silence(self):
        self.save([
            mido.MetaMessage("track_name", name="Melody"),
            mido.Message("note_on", note=60, velocity=90, time=240),
            mido.Message("note_off", note=60, time=480),
            mido.Message("note_on", note=62, velocity=70, time=240),
            mido.Message("note_off", note=62, time=240),
            mido.MetaMessage("end_of_track", time=240),
        ])
        data = load_midi(self.path)
        self.assertEqual(len(data.notes), 2)
        self.assertAlmostEqual(data.notes[0].start, 0.25)
        self.assertAlmostEqual(data.notes[0].end, 0.75)
        self.assertAlmostEqual(data.notes[1].start, 1.0)
        self.assertAlmostEqual(data.notes[1].end, 1.25)
        self.assertAlmostEqual(data.duration, 1.5)
        self.assertEqual(data.tracks[0].name, "Melody")
        self.assertEqual(data.tracks[0].channels, (0,))
        self.assertEqual(data.tracks[0].note_count, 2)
        self.assertFalse(data.warnings)

    def test_tempo_changes_in_another_track_apply_inside_held_note(self):
        self.save([
            mido.MetaMessage("set_tempo", tempo=500_000),
            mido.MetaMessage("set_tempo", tempo=1_000_000, time=480),
            mido.MetaMessage("end_of_track", time=960),
        ], [
            mido.Message("note_on", note=60, velocity=100, time=240),
            mido.Message("note_off", note=60, time=720),
        ])
        data = load_midi(self.path, track=1)
        self.assertAlmostEqual(data.notes[0].start, 0.25)
        self.assertAlmostEqual(data.notes[0].end, 1.5)
        self.assertAlmostEqual(data.notes[0].duration, 1.25)
        self.assertAlmostEqual(data.duration, 2.5)
        self.assertEqual(len(data.tracks), 2)

    def test_a_late_first_tempo_does_not_retroactively_apply(self):
        self.save([
            mido.Message("note_on", note=60, velocity=90),
            mido.MetaMessage("set_tempo", tempo=1_000_000, time=480),
            mido.Message("note_off", note=60, time=480),
        ], midi_type=0)
        self.assertAlmostEqual(load_midi(self.path).notes[0].duration, 1.5)

    def test_very_short_notes_keep_full_precision(self):
        self.save([
            mido.Message("note_on", note=60, velocity=90),
            mido.Message("note_off", note=60, time=1),
        ], midi_type=0)
        note = load_midi(self.path).notes[0]
        self.assertAlmostEqual(note.duration, 1 / 960)
        self.assertGreater(note.duration, 0)

    def test_overlapping_same_pitch_uses_fifo_pairing(self):
        self.save([
            mido.Message("note_on", note=60, velocity=80),
            mido.Message("note_on", note=60, velocity=100, time=240),
            mido.Message("note_off", note=60, time=240),
            mido.Message("note_on", note=60, velocity=0, time=240),
        ])
        first, second = load_midi(self.path).notes
        self.assertAlmostEqual(first.start, 0)
        self.assertAlmostEqual(first.end, 0.5)
        self.assertEqual(first.velocity, 80)
        self.assertAlmostEqual(second.start, 0.25)
        self.assertAlmostEqual(second.end, 0.75)
        self.assertEqual(second.velocity, 100)

    def test_track_and_channel_selection_keeps_absolute_timing(self):
        self.save([
            mido.MetaMessage("track_name", name="Conductor"),
            mido.MetaMessage("set_tempo", tempo=1_000_000),
            mido.MetaMessage("end_of_track", time=1920),
        ], [
            mido.Message("note_on", note=60, velocity=90, channel=0, time=480),
            mido.Message("note_on", note=64, velocity=90, channel=1),
            mido.Message("note_off", note=60, channel=0, time=480),
            mido.Message("note_off", note=64, channel=1),
        ])
        data = load_midi(self.path, track=1, channel=1)
        self.assertEqual([note.pitch for note in data.notes], [64])
        self.assertAlmostEqual(data.notes[0].start, 1)
        self.assertAlmostEqual(data.notes[0].end, 2)
        self.assertAlmostEqual(data.duration, 4)
        self.assertEqual(data.tracks[1].channels, (0, 1))
        self.assertEqual(data.tracks[1].note_count, 2)
        self.assertEqual(load_midi(self.path, track=0).notes, ())

    def test_same_pitch_in_separate_tracks_does_not_cross_pair(self):
        self.save([
            mido.Message("note_on", note=60, velocity=80),
            mido.Message("note_off", note=60, time=960),
        ], [
            mido.Message("note_on", note=60, velocity=90, time=240),
            mido.Message("note_off", note=60, time=240),
        ])
        data = load_midi(self.path)
        self.assertEqual([(note.track, note.start, note.end) for note in data.notes], [(0, 0, 1), (1, 0.25, 0.5)])

    def test_sustain_release_extends_notes_until_pedal_up(self):
        self.save([
            mido.Message("control_change", control=64, value=127),
            mido.Message("note_on", note=60, velocity=90),
            mido.Message("note_off", note=60, time=240),
            mido.Message("control_change", control=64, value=0, time=720),
        ])
        self.assertAlmostEqual(load_midi(self.path).notes[0].end, 1)

    def test_pedal_in_another_track_still_applies_after_filtering(self):
        self.save([
            mido.Message("control_change", control=64, value=127),
            mido.Message("control_change", control=64, value=0, time=960),
        ], [
            mido.Message("note_on", note=60, velocity=90),
            mido.Message("note_off", note=60, time=240),
        ])
        self.assertAlmostEqual(load_midi(self.path, track=1).notes[0].end, 1)

    def test_all_sound_off_stops_sustained_and_held_notes(self):
        self.save([
            mido.Message("control_change", control=64, value=127),
            mido.Message("note_on", note=60, velocity=90),
            mido.Message("note_on", note=64, velocity=90),
            mido.Message("note_off", note=60, time=240),
            mido.Message("control_change", control=120, value=0, time=240),
            mido.MetaMessage("end_of_track", time=480),
        ])
        self.assertEqual([note.end for note in load_midi(self.path).notes], [0.5, 0.5])

    def test_unmatched_noteoff_and_unclosed_note_warn_without_crashing(self):
        self.save([
            mido.Message("note_off", note=65),
            mido.Message("note_on", note=60, velocity=90, time=240),
            mido.MetaMessage("end_of_track", time=720),
        ])
        data = load_midi(self.path)
        self.assertAlmostEqual(data.notes[0].start, 0.25)
        self.assertAlmostEqual(data.notes[0].end, 1)
        self.assertEqual(len(data.warnings), 2)

    def test_zero_duration_note_is_ignored_with_warning(self):
        self.save([
            mido.Message("note_on", note=60, velocity=90),
            mido.Message("note_off", note=60),
        ])
        data = load_midi(self.path)
        self.assertFalse(data.notes)
        self.assertTrue(data.warnings)

    def test_legacy_duration_interface_and_pitch_name(self):
        self.save([
            mido.Message("note_on", note=61, velocity=90, time=240),
            mido.Message("note_off", note=61, time=480),
        ])
        self.assertEqual(midi_note_to_durations(self.path), [["C#4", 0.5]])
        self.assertEqual(note_name(0), "C-1")
        self.assertEqual(note_name(60), "C4")
        self.assertEqual(note_name(127), "G9")
        for invalid in (-1, 128, 60.5, True, "60"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                note_name(invalid)
        self.assertAlmostEqual(time_in_s(1, 500_000, 480), 1 / 960)

    def test_invalid_track_and_channel_fail_clearly(self):
        self.save([mido.MetaMessage("end_of_track")])
        for invalid in (-1, 1, True, 0.5, "0"):
            with self.subTest(track=invalid), self.assertRaises(ValueError):
                load_midi(self.path, track=invalid)
        for invalid in (-1, 16, True, 0.5, "0"):
            with self.subTest(channel=invalid), self.assertRaises(ValueError):
                load_midi(self.path, channel=invalid)

    def test_type_two_and_smpte_are_rejected(self):
        self.save([mido.MetaMessage("end_of_track")], midi_type=2)
        with self.assertRaisesRegex(ValueError, "Type 2"):
            load_midi(self.path)
        self.save([mido.MetaMessage("end_of_track")], ticks_per_beat=-6360)
        with self.assertRaisesRegex(ValueError, "SMPTE"):
            load_midi(self.path)

    def test_empty_valid_file_and_invalid_file(self):
        self.save([mido.MetaMessage("end_of_track", time=480)])
        data = load_midi(self.path)
        self.assertEqual(data.notes, ())
        self.assertAlmostEqual(data.duration, 0.5)
        self.path.write_bytes(b"not a midi file")
        with self.assertRaisesRegex(ValueError, "无法读取"):
            load_midi(self.path)
        with self.assertRaisesRegex(ValueError, "无法读取"):
            load_midi(Path(self.directory.name) / "missing.mid")


if __name__ == "__main__":
    unittest.main()
