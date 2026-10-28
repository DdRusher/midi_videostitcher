"""Source windows and random excerpts share one reproducible AV plan."""

from dataclasses import FrozenInstanceError, replace
import math
import random
import unittest

from material_selection import MaterialSelection, material_windows, plan_material_offsets
from notes import MidiData, NoteEvent, TrackInfo


class MaterialWindowTests(unittest.TestCase):
    def test_defaults_use_whole_material_and_end_none_uses_actual_end(self):
        self.assertEqual(material_windows(None, {60: 5, 61: 4}), {60: (0, 5), 61: (0, 4)})
        self.assertEqual(material_windows({60: MaterialSelection(2)}, {60: 5}), {60: (2, 5)})

    def test_fixed_or_random_windows_are_absolute_source_times(self):
        settings = {60: MaterialSelection(1, 3), 61: MaterialSelection(2, 5, "random")}
        self.assertEqual(material_windows(settings, {60: 5, 61: 7}), {60: (1, 3), 61: (2, 5)})

    def test_unused_settings_do_not_require_their_source_or_lose_selected_window(self):
        settings = {60: MaterialSelection(1, 3), 61: MaterialSelection(8, 10)}
        self.assertEqual(material_windows(settings, {60: 5}), {60: (1, 3)})
        self.assertEqual(settings[61], MaterialSelection(8, 10))

    def test_selection_is_immutable(self):
        setting = MaterialSelection(1, 3, "random")
        with self.assertRaises(FrozenInstanceError):
            setting.start = 2

    def test_invalid_pitch_keys_and_settings_fail_clearly(self):
        for pitch in (-1, 128, True, 60.0, "60"):
            with self.subTest(pitch=pitch), self.assertRaisesRegex(ValueError, "MIDI 整数"):
                material_windows({pitch: MaterialSelection()}, {60: 5})
            with self.subTest(duration_pitch=pitch), self.assertRaisesRegex(ValueError, "MIDI 整数"):
                material_windows(None, {pitch: 5})
        with self.assertRaisesRegex(ValueError, "MaterialSelection"):
            material_windows({60: {"start": 0}}, {60: 5})
        with self.assertRaisesRegex(ValueError, "选段设置"):
            material_windows([], {60: 5})
        with self.assertRaisesRegex(ValueError, "素材时长"):
            material_windows(None, [5])

    def test_invalid_ranges_modes_and_durations_fail_with_note_name(self):
        invalid = (
            MaterialSelection(-1), MaterialSelection(1, 1), MaterialSelection(2, 1),
            MaterialSelection(0, 6), MaterialSelection(5), MaterialSelection(math.nan),
            MaterialSelection(math.inf), MaterialSelection(0, math.inf),
            MaterialSelection(False), MaterialSelection(0, True), MaterialSelection(0, 5, "shuffle"),
        )
        for setting in invalid:
            with self.subTest(setting=setting), self.assertRaisesRegex(ValueError, "C4"):
                material_windows({60: setting}, {60: 5})
        for duration in (0, -1, math.nan, math.inf, True, None):
            with self.subTest(duration=duration), self.assertRaisesRegex(ValueError, "C4"):
                material_windows(None, {60: duration})


class MaterialOffsetTests(unittest.TestCase):
    def setUp(self):
        self.data = MidiData(
            (NoteEvent(60, 0, 2, 31, 0, 1), NoteEvent(60, 0.5, 2.5, 115, 1, 2),
             NoteEvent(64, 3, 4, 80, 0, 1)),
            (TrackInfo(1, "Piano", (0,), 2), TrackInfo(2, "Other", (1,), 1)),
            5.0, ("retained warning",),
        )
        self.durations = {60: 5.0, 64: 5.0}

    def test_default_offsets_and_all_metadata_are_preserved(self):
        result = plan_material_offsets(self.data, None, self.durations)
        self.assertEqual(result, self.data)
        self.assertIsNot(result, self.data)
        self.assertIs(result.tracks, self.data.tracks)
        self.assertIs(result.warnings, self.data.warnings)
        self.assertEqual([note.velocity for note in result.notes], [31, 115, 80])

    def test_fixed_offset_is_relative_to_physically_selected_window(self):
        result = plan_material_offsets(self.data, {60: MaterialSelection(2, 4)}, self.durations)
        self.assertEqual([note.source_offset for note in result.notes], [0, 0, 0])
        self.assertEqual(material_windows({60: MaterialSelection(2, 4)}, self.durations)[60], (2, 4))

    def test_random_repeats_with_seed_and_changes_with_another_seed(self):
        settings = {60: MaterialSelection(0, 5, "random"), 64: MaterialSelection(1, 4, "random")}
        first = plan_material_offsets(self.data, settings, self.durations, 123)
        self.assertEqual(first, plan_material_offsets(self.data, settings, self.durations, 123))
        other = plan_material_offsets(self.data, settings, self.durations, 124)
        self.assertNotEqual(first.notes, other.notes)
        for note in first.notes:
            width = 5 if note.pitch == 60 else 3
            self.assertGreaterEqual(note.source_offset, 0)
            self.assertLessEqual(note.source_offset + note.duration, width)
        self.assertNotEqual(first.notes[0].source_offset, first.notes[1].source_offset)

    def test_random_planning_does_not_change_global_rng_or_caller_objects(self):
        settings = {60: MaterialSelection(0, 5, "random")}
        before_state = random.getstate()
        original_notes = self.data.notes
        original_settings = settings.copy()
        plan_material_offsets(self.data, settings, self.durations, -8)
        self.assertEqual(random.getstate(), before_state)
        self.assertIs(self.data.notes, original_notes)
        self.assertEqual(settings, original_settings)

    def test_crop_continuation_is_preserved_in_fixed_and_random_modes(self):
        cropped = replace(self.data, notes=(replace(self.data.notes[0], end=1, source_offset=1.5),))
        fixed = plan_material_offsets(cropped, {60: MaterialSelection(1, 4)}, {60: 5})
        self.assertEqual(fixed.notes[0].source_offset, 1.5)
        randomized = plan_material_offsets(cropped, {60: MaterialSelection(1, 4, "random")}, {60: 5}, 29)
        self.assertGreaterEqual(randomized.notes[0].source_offset, 1.5)
        self.assertLessEqual(randomized.notes[0].source_offset + 1, 3)

    def test_short_window_loops_without_losing_long_continuation(self):
        data = replace(self.data, notes=(replace(self.data.notes[0], source_offset=8),))
        for mode in ("fixed", "random"):
            with self.subTest(mode=mode):
                result = plan_material_offsets(data, {60: MaterialSelection(2, 3, mode)}, {60: 5}, 7)
                self.assertEqual(result.notes[0].source_offset, 8)
                self.assertEqual(result.notes[0].duration, 2)

    def test_note_exactly_fitting_window_has_no_random_displacement(self):
        result = plan_material_offsets(self.data, {60: MaterialSelection(1, 3, "random")}, self.durations, 1)
        self.assertEqual([note.source_offset for note in result.notes[:2]], [0, 0])

    def test_missing_selected_duration_and_invalid_seed_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "缺少 E4"):
            plan_material_offsets(self.data, None, {60: 5})
        for seed in (True, 1.2, "1", None):
            with self.subTest(seed=seed), self.assertRaisesRegex(ValueError, "种子必须是整数"):
                plan_material_offsets(self.data, None, self.durations, seed)

    def test_invalid_note_offsets_and_note_intervals_fail_clearly(self):
        for offset in (-1, math.inf, math.nan, True):
            data = replace(self.data, notes=(replace(self.data.notes[0], source_offset=offset),))
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "C4"):
                plan_material_offsets(data, None, self.durations)
        for start, end in ((-1, 1), (1, 1), (2, 1), (0, math.inf)):
            data = replace(self.data, notes=(replace(self.data.notes[0], start=start, end=end),))
            with self.subTest(start=start, end=end), self.assertRaisesRegex(ValueError, "C4"):
                plan_material_offsets(data, None, self.durations)


if __name__ == "__main__":
    unittest.main()
