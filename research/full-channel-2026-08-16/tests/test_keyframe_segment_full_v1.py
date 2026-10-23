from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "keyframe_segment_full_v1.py"
)


class KeyframePlannerTests(unittest.TestCase):
    def load_module(self):
        self.assertTrue(SCRIPT.is_file(), "versioned implementation must exist")
        spec = importlib.util.spec_from_file_location("keyframe_segment_full_v1", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        return module

    def test_plan_uses_latest_keyframe_without_exceeding_limit(self):
        module = self.load_module()
        sections = module.plan_keyframe_sections(
            Decimal("1280.000000"),
            [
                Decimal("0.000000"),
                Decimal("594.500000"),
                Decimal("601.500000"),
                Decimal("1189.500000"),
                Decimal("1194.600000"),
            ],
            Decimal("600.000000"),
        )

        self.assertEqual(
            [(row["start_seconds"], row["end_seconds"]) for row in sections],
            [
                ("0.000000", "594.500000"),
                ("594.500000", "1189.500000"),
                ("1189.500000", "1280.000000"),
            ],
        )
        self.assertTrue(
            all(Decimal(row["duration_seconds"]) <= Decimal("600") for row in sections)
        )

    def test_plan_fails_closed_when_no_keyframe_can_keep_section_bounded(self):
        module = self.load_module()
        with self.assertRaisesRegex(RuntimeError, "keyframe"):
            module.plan_keyframe_sections(
                Decimal("1300.000000"),
                [Decimal("0.000000"), Decimal("650.000000")],
                Decimal("600.000000"),
            )


if __name__ == "__main__":
    unittest.main()
