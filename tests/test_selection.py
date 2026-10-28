"""Track/channel/range selection keeps note timing and source-media position."""

from dataclasses import replace
import unittest

from notes import MidiData, NoteEvent, TrackInfo
from selection import parse_time, select_midi


class ParseTimeTests(unittest.TestCase):
    def test_seconds_minutes_and_hours(self):
        for text, seconds in (
            ("0", 0.0),
            ("12.5", 12.5),
            (".25", 0.25),
            (" 01:02.345 ", 62.345),
            ("90:00", 5400.0),
            ("1:02:03.5", 3723.5),
            ("00:00:00.001", 0.001),
        ):
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_time(text), seconds)

    def test_invalid_negative_nonfinite_or_malformed_values_are_rejected(self):
        invalid = (
            "", " ", "-1", "-0", "-01:00", "1:-2", "1:60", "1:99.1",
            "1:60:00", "01:00:60", "1:2:3:4", "1::3", "1:", ":2",
            "1.5:02", "1:2.5:03", "NaN", "nan", "Infinity", "inf",
            "1e309", "12 seconds", None, 1.5, "9" * 400,
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_time(text)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tracks = (
            TrackInfo(0, "Conductor", (), 0),
            TrackInfo(1, "Melody", (0, 1), 3),
            TrackInfo(2, "Bass", (2,), 1),
        )
        self.data = MidiData(
            (
                NoteEvent(60, 1, 5, 90, 0, 1),
                NoteEvent(67, 2, 4, 75, 2, 2),
                NoteEvent(64, 3, 7, 80, 0, 1),
                NoteEvent(60, 4, 8, 100, 1, 1),
            ),
            self.tracks,
            10.0,
            ("示例提示",),
        )

    def test_default_selects_all_and_preserves_full_file_metadata(self):
        result = select_midi(self.data)
        self.assertEqual(result.notes, self.data.notes)
        self.assertEqual(result.duration, 10)
        self.assertIs(result.tracks, self.data.tracks)
        self.assertIs(result.warnings, self.data.warnings)
        self.assertTrue(all(note.source_offset == 0 for note in result.notes))

    def test_track_and_channel_filters_intersect(self):
        result = select_midi(self.data, tracks=[1], channels=[0])
        self.assertEqual([note.pitch for note in result.notes], [60, 64])
        self.assertEqual([note.start for note in result.notes], [1, 3])
        self.assertEqual(result.duration, 10)

    def test_multiple_tracks_and_channels_support_generators_and_duplicates(self):
        result = select_midi(
            self.data,
            tracks=(index for index in [1, 2, 1]),
            channels=(channel for channel in [1, 2]),
        )
        self.assertEqual([(note.track, note.channel) for note in result.notes], [(2, 2), (1, 1)])

    def test_middle_cut_clips_both_ends_and_retains_source_position(self):
        result = select_midi(self.data, start=2, end=6)
        self.assertEqual(result.duration, 4)
        self.assertEqual(
            [(note.pitch, note.start, note.end, note.source_offset) for note in result.notes],
            [(60, 0, 3, 1), (67, 0, 2, 0), (64, 1, 4, 0), (60, 2, 4, 0)],
        )
        self.assertEqual(self.data.notes[0].start, 1)
        self.assertEqual(self.data.notes[0].source_offset, 0)

    def test_selection_starting_before_a_note_does_not_skip_its_source(self):
        result = select_midi(self.data, tracks=[1], channels=[0], start=0.5, end=3.5)
        self.assertEqual([(note.start, note.end, note.source_offset) for note in result.notes], [
            (0.5, 3.0, 0.0), (2.5, 3.0, 0.0),
        ])

    def test_leading_and_trailing_rests_remain_in_selection_duration(self):
        data = MidiData((NoteEvent(60, 3, 4, 90, 0, 1),), self.tracks, 10, ())
        result = select_midi(data, start=1, end=7)
        self.assertEqual(result.duration, 6)
        self.assertEqual((result.notes[0].start, result.notes[0].end), (2, 3))

    def test_overlapping_same_pitch_notes_are_not_merged(self):
        data = MidiData((
            NoteEvent(60, 1, 5, 80, 0, 1),
            NoteEvent(60, 2, 6, 100, 0, 1),
        ), self.tracks, 10, ())
        result = select_midi(data, start=3, end=4)
        self.assertEqual(len(result.notes), 2)
        self.assertEqual([(note.start, note.end) for note in result.notes], [(0, 1), (0, 1)])
        self.assertEqual([note.source_offset for note in result.notes], [2, 1])
        self.assertEqual([note.velocity for note in result.notes], [80, 100])

    def test_second_cut_accumulates_offset_and_matches_a_direct_cut(self):
        data = MidiData((NoteEvent(60, 1, 8, 90, 0, 1, source_offset=2.5),), self.tracks, 10, ())
        first = select_midi(data, start=3, end=7)
        second = select_midi(first, start=1, end=3)
        direct = select_midi(data, start=4, end=6)
        self.assertEqual(second, direct)
        self.assertEqual(second.notes[0].source_offset, 5.5)
        self.assertEqual(first.notes[0].source_offset, 4.5)
        self.assertEqual(data.notes[0].source_offset, 2.5)

    def test_exact_right_boundary_excludes_new_notes_and_left_boundary_excludes_ended_notes(self):
        data = MidiData((
            NoteEvent(60, 1, 2, 90, 0, 1),
            NoteEvent(62, 2, 3, 90, 0, 1),
            NoteEvent(64, 3, 4, 90, 0, 1),
        ), self.tracks, 5, ())
        result = select_midi(data, start=2, end=3)
        self.assertEqual([note.pitch for note in result.notes], [62])
        self.assertEqual((result.notes[0].start, result.notes[0].end), (0, 1))

    def test_end_none_means_full_remaining_duration(self):
        result = select_midi(self.data, start=4)
        self.assertEqual(result.duration, 6)
        self.assertEqual(result.notes[0].source_offset, 3)
        self.assertTrue(all(note.start >= 0 and note.end <= 6 for note in result.notes))

    def test_selection_does_not_round_very_short_notes(self):
        data = MidiData((NoteEvent(60, 1, 1.0001, 90, 0, 1),), self.tracks, 2, ())
        note = select_midi(data, start=0.5, end=1.5).notes[0]
        self.assertAlmostEqual(note.duration, 0.0001)
        self.assertAlmostEqual(note.start, 0.5)

    def test_pure_rest_range_or_empty_filter_result_is_explained(self):
        for kwargs in (
            {"start": 8.5, "end": 9.5},
            {"tracks": [0]},
            {"channels": [15]},
            {"tracks": [2], "channels": [0]},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "没有可用音符"):
                select_midi(self.data, **kwargs)

    def test_empty_track_or_channel_lists_are_an_error(self):
        for kwargs, label in (({"tracks": []}, "轨道"), ({"channels": []}, "通道")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, label):
                select_midi(self.data, **kwargs)

    def test_track_indices_must_exist_and_channel_indices_are_zero_based(self):
        for kwargs in (
            {"tracks": [-1]}, {"tracks": [3]}, {"tracks": [True]}, {"tracks": [1.0]},
            {"tracks": ["1"]}, {"tracks": 1},
            {"channels": [-1]}, {"channels": [16]}, {"channels": [False]},
            {"channels": [0.0]}, {"channels": ["0"]}, {"channels": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                select_midi(self.data, **kwargs)

    def test_invalid_time_ranges_are_rejected(self):
        for kwargs in (
            {"start": -1}, {"start": 10}, {"start": 11},
            {"start": 1, "end": 1}, {"start": 2, "end": 1}, {"end": 0},
            {"end": 10.0001}, {"start": float("nan")}, {"end": float("inf")},
            {"start": True}, {"end": "not a time"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                select_midi(self.data, **kwargs)

    def test_invalid_source_offsets_and_invalid_song_duration_are_rejected(self):
        for offset in (-1, float("nan"), float("inf")):
            data = replace(self.data, notes=(replace(self.data.notes[0], source_offset=offset),))
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "偏移"):
                select_midi(data)
        for duration in (0, -1, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                select_midi(replace(self.data, duration=duration))


if __name__ == "__main__":
    unittest.main()
