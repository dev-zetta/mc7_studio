import unittest

from swarm2.calibration import (AngleCalibrationSession, DpiSample,
                                angle_stroke_degrees, dpi_sample_value, suggest_dpi)


class CalibrationTests(unittest.TestCase):
    def test_angle_folds_left_passes_and_truncates_toward_zero(self):
        self.assertEqual(angle_stroke_degrees((0, 0), (1000, 100)), 5)
        self.assertEqual(angle_stroke_degrees((1000, 100), (0, 0)), 5)
        self.assertEqual(angle_stroke_degrees((0, 100), (1000, 0)), -5)
        self.assertEqual(angle_stroke_degrees((1000, 0), (0, 100)), -5)

    def test_angle_mean_is_integer_truncation_before_negation(self):
        session = AngleCalibrationSession((0, 0))
        x = y = 0
        for index in range(10):
            direction = 1 if index % 2 == 0 else -1
            x += 1000 * direction
            y -= (100 if index < 9 else 87) * direction
            session.add_endpoint((x, y))
        result = session.result()
        self.assertEqual(result.stroke_angles, (-5,)*9 + (-4,))
        self.assertEqual((result.mean_angle, result.suggested_angle), (-4, 4))
        with self.assertRaisesRegex(ValueError, "already complete"):
            session.add_endpoint((1000, 0))

    def test_angle_clamps_to_mouse_range_after_full_result(self):
        session = AngleCalibrationSession((0, 0))
        for index in range(10):
            session.add_endpoint((1000, 100000) if index % 2 == 0 else (0, 0))
        result = session.result()
        self.assertEqual((result.unclamped_angle, result.suggested_angle), (-89, -30))

    def test_invalid_direction_short_span_partial_and_practice_cannot_make_result(self):
        session = AngleCalibrationSession((0, 0))
        for endpoint in ((-100, 0), (0, 100), (31, 100), (True, 100), (2_000_000, 1)):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                session.add_endpoint(endpoint)
        self.assertEqual(session.angles, ())
        with self.assertRaises(ValueError):
            session.result()
        practice = AngleCalibrationSession((0, 0), strokes=3)
        for point in ((100, 1), (0, 0), (100, 1)):
            practice.add_endpoint(point)
        self.assertTrue(practice.complete)
        with self.assertRaises(ValueError):
            practice.result()

    def test_dpi_uses_target_over_travel_ratio_and_reports_pointer_error(self):
        sample = DpiSample((0, 0), (1000, 0), (800, 0))
        result = suggest_dpi(1000, [sample]*5)
        self.assertEqual(result.sample_dpi, (1250,)*5)
        self.assertEqual((result.suggested_dpi, result.accuracy_pixels, result.precision_pixels), (1250, 200, 0))
        self.assertEqual(dpi_sample_value(1000, DpiSample((0, 0), (300, 400), (600, 800))), 500)

    def test_dpi_exact_half_step_rounds_down_and_next_integer_rounds_up(self):
        for distance, expected in ((1025, 1000), (1026, 1050), (1000, 1000)):
            with self.subTest(distance=distance):
                sample = DpiSample((0, 0), (distance, 0), (1000, 0))
                result = suggest_dpi(1000, [sample]*5)
                self.assertEqual(result.mean_dpi, distance)
                self.assertEqual(result.suggested_dpi, expected)

    def test_dpi_clamps_each_sample_before_averaging(self):
        high = DpiSample((0, 0), (30000, 0), (32, 0))
        low = DpiSample((0, 0), (32, 0), (30000, 0))
        result = suggest_dpi(1000, [high, low, low, low, low])
        self.assertEqual(result.sample_dpi, (30000, 50, 50, 50, 50))
        self.assertEqual((result.mean_dpi, result.suggested_dpi), (6040, 6050))

    def test_dpi_rejects_zero_short_or_unbounded_samples_and_partial_runs(self):
        for sample in (DpiSample((0, 0), (100, 0), (0, 0)),
                       DpiSample((0, 0), (31, 0), (100, 0)),
                       DpiSample((0, 0), (100, 0), (10, 10)),
                       DpiSample((0, 0), (float("nan"), 0), (100, 0))):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                dpi_sample_value(1000, sample)
        sample = DpiSample((0, 0), (100, 0), (100, 0))
        for samples in ([], [sample]*4, [sample]*6):
            with self.assertRaises(ValueError):
                suggest_dpi(1000, samples)
        for dpi in (True, 0, 1001, 30050, 1000.0):
            with self.assertRaises(ValueError):
                suggest_dpi(dpi, [sample]*5)


if __name__ == "__main__":
    unittest.main()
