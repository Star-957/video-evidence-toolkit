import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from verify_visual_attempt import frame_tree, verify_csv_sheets, verify_frames


class VerifyVisualAttemptTests(unittest.TestCase):
    def test_accepts_requested_target_start_in_csv_sheet_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            contact_sheets = Path(temporary) / "contact-sheets"
            chunk = contact_sheets / "chunk_0000"
            chunk.mkdir(parents=True)
            payload = b"sheet"
            sheet = chunk / "sheet_00001.jpg"
            sheet.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest().upper()
            (contact_sheets / "index.csv").write_text(
                "sequence,chunk,sheet,requested_target_start,requested_target_end,"
                "actual_pts_start,actual_pts_end,source_frame_start,"
                "source_frame_end,sha256\n"
                f"0,chunk_0000,sheet_00001.jpg,0,4.75,0,4.766667,0,19,{digest}\n",
                encoding="utf-8",
            )

            errors = []
            sheet_count = verify_csv_sheets(
                contact_sheets / "index.csv", 20, errors
            )

            self.assertEqual(sheet_count, 1)
            self.assertEqual(errors, [])

    def test_accepts_real_source_frame_within_extractor_sampling_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary)
            chunk = attempt / "visual-4fps" / "chunk-0000"
            frames_dir = chunk / "frames"
            frames_dir.mkdir(parents=True)
            frames = []
            for sequence in range(6):
                payload = f"frame-{sequence}".encode("ascii")
                frame_path = frames_dir / f"frame_{sequence:08d}.jpg"
                frame_path.write_bytes(payload)
                target = sequence / 4
                source_pts = sequence * 0.25025
                frames.append(
                    {
                        "sequence": sequence,
                        "relative_path": f"frames/{frame_path.name}",
                        "source_pts_seconds": source_pts,
                        "sample_target_seconds": target,
                        "sampling_error_seconds": source_pts - target,
                        "width": 448,
                        "height": 252,
                        "sha256": hashlib.sha256(payload).hexdigest().upper(),
                    }
                )
            manifest = {
                "pipeline_version": "visual-chunk-v5",
                "settings": {
                    "baseline_fps": 4,
                    "long_edge": 448,
                    "interpolation_allowed": False,
                    "start_seconds": 0.0,
                },
                "source": {
                    "hash_stable_during_extraction": True,
                    "sha256": "A" * 64,
                    "avg_fps": 60000 / 1001,
                    "nominal_fps": 60000 / 1001,
                },
                "frames": frames,
                "frames_tree_sha256": frame_tree(frames),
            }
            (chunk / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            errors = []
            frame_count, _ = verify_frames(attempt, errors)

            self.assertEqual(frame_count, 6)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
