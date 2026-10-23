from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acquire_longform_sections_v2.py"


class LongformVideoOnlySelectorV2Tests(unittest.TestCase):
    def load_module(self):
        self.assertTrue(SCRIPT.is_file(), "versioned v2 implementation must exist")
        spec = importlib.util.spec_from_file_location("acquire_longform_sections_v2", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_full_and_section_acquisition_require_a_video_only_format(self):
        module = self.load_module()
        expected = "bv[height<=360]"
        self.assertEqual(module.POLICY["source_format_selector"], expected)
        full = module.build_ytdlp_full_command("yt-dlp.exe")
        section = module.build_ytdlp_section_command(
            "yt-dlp.exe",
            {"start_seconds": "0.000000", "end_seconds": "600.000000"},
        )
        self.assertEqual(full[full.index("--format") + 1], expected)
        self.assertEqual(section[section.index("--format") + 1], expected)
        self.assertNotIn("18/b[height<=360]", full)
        self.assertNotIn("18/b[height<=360]", section)


if __name__ == "__main__":
    unittest.main()
