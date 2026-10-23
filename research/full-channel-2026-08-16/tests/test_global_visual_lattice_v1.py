from fractions import Fraction
import importlib.util
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "global_visual_lattice_v1.py"
)
sys.path.insert(0, str(SCRIPT.parent))


class GlobalVisualLatticePlannerTests(unittest.TestCase):
    def load_module(self):
        self.assertTrue(SCRIPT.is_file(), "add-only global lattice implementation must exist")
        spec = importlib.util.spec_from_file_location("global_visual_lattice_v1", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        return module

    def test_end_boundary_frame_is_owned_by_next_section_without_gap_or_duplicate(self):
        module = self.load_module()
        frame_pts = [Fraction(index, 30) for index in range(60)]
        boundary = Fraction(23, 30)
        sections = [
            (0, Fraction(0), boundary),
            (1, boundary, Fraction(2)),
        ]

        rows = module.plan_global_lattice(
            frame_pts=frame_pts,
            source_duration=Fraction(2),
            baseline_fps=4,
            sections=sections,
        )

        self.assertEqual([row["target_index"] for row in rows], list(range(8)))
        self.assertEqual(len({row["target_index"] for row in rows}), 8)
        self.assertEqual(rows[3]["target_seconds"], Fraction(3, 4))
        self.assertEqual(rows[3]["selected_source_pts_seconds"], boundary)
        self.assertEqual(rows[3]["owner_section_index"], 1)
        self.assertEqual(
            [row["target_index"] for row in rows if row["owner_section_index"] == 0],
            [0, 1, 2],
        )
        self.assertEqual(
            [row["target_index"] for row in rows if row["owner_section_index"] == 1],
            [3, 4, 5, 6, 7],
        )

    def test_resolves_six_decimal_plan_boundary_to_exact_source_pts(self):
        module = self.load_module()
        self.assertTrue(
            hasattr(module, "resolve_plan_sections"),
            "rounded section resolver must exist",
        )
        frame_pts = [Fraction(index, 30) for index in range(60)]
        plan_rows = [
            {"index": 0, "start_seconds": "0.000000", "end_seconds": "0.766667"},
            {"index": 1, "start_seconds": "0.766667", "end_seconds": "2.000000"},
        ]

        sections = module.resolve_plan_sections(
            plan_rows=plan_rows,
            frame_pts=frame_pts,
            source_duration=Fraction(2),
            tolerance=Fraction(1, 1_000_000),
        )

        self.assertEqual(
            sections,
            [
                (0, Fraction(0), Fraction(23, 30)),
                (1, Fraction(23, 30), Fraction(2)),
            ],
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class GlobalVisualLatticeExtractionTests(unittest.TestCase):
    def load_module(self):
        self.assertTrue(SCRIPT.is_file())
        spec = importlib.util.spec_from_file_location("global_visual_lattice_v1", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        return module

    def make_video(self, path: Path) -> None:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=30:duration=2",
                "-an",
                "-c:v",
                "libx264",
                "-g",
                "1",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_extracts_only_global_targets_whose_selected_frames_belong_to_section(self):
        module = self.load_module()
        self.assertTrue(
            hasattr(module, "probe_global_source"),
            "global source probe must exist",
        )
        self.assertTrue(
            hasattr(module, "run_global_section_extract"),
            "global section extractor must exist",
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            output = Path(tmp) / "global-section-1"
            self.make_video(source)
            probe = module.probe_global_source(source)
            boundary = Fraction(23, 30)
            sections = [
                (0, Fraction(0), boundary),
                (1, boundary, probe["duration"]),
            ]
            rows = module.plan_global_lattice(
                frame_pts=probe["frame_pts"],
                source_duration=probe["duration"],
                baseline_fps=4,
                sections=sections,
            )
            expected = [row for row in rows if row["owner_section_index"] == 1]

            manifest = module.run_global_section_extract(
                source=source,
                output_dir=output,
                section=(1, boundary, probe["duration"]),
                expected_rows=expected,
                baseline_fps=4,
                long_edge=320,
                max_frames=100,
            )

            self.assertEqual(manifest["pipeline_version"], "global-visual-lattice-v1")
            self.assertEqual(
                [frame["global_target_index"] for frame in manifest["frames"]],
                [row["target_index"] for row in expected],
            )
            self.assertEqual(
                [Fraction(frame["global_target_fraction"]) for frame in manifest["frames"]],
                [row["target_seconds"] for row in expected],
            )
            self.assertEqual(
                [Fraction(frame["source_pts_fraction"]) for frame in manifest["frames"]],
                [row["selected_source_pts_seconds"] for row in expected],
            )
            self.assertTrue(
                all(frame["owner_section_index"] == 1 for frame in manifest["frames"])
            )
            self.assertEqual(
                manifest,
                json.loads((output / "manifest.json").read_text(encoding="utf-8")),
            )

    def test_writes_atomic_contract_with_complete_global_target_ledger(self):
        module = self.load_module()
        self.assertTrue(
            hasattr(module, "write_global_contract"),
            "atomic global contract writer must exist",
        )
        self.assertIn(
            "research_contract_sha256",
            inspect.signature(module.write_global_contract).parameters,
            "global visual contract must bind the active research contract hash",
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            output = Path(tmp) / "global-contract"
            self.make_video(source)
            plan_rows = [
                {"index": 0, "start_seconds": "0.000000", "end_seconds": "0.766667"},
                {"index": 1, "start_seconds": "0.766667", "end_seconds": "2.000000"},
            ]

            contract = module.write_global_contract(
                source=source,
                output_dir=output,
                plan_rows=plan_rows,
                baseline_fps=4,
                long_edge=320,
                plan_sha256="A" * 64,
                research_contract_sha256="C" * 64,
            )

            self.assertEqual(contract["pipeline_version"], "global-visual-lattice-v1")
            self.assertEqual(contract["global_lattice"]["target_count"], 8)
            self.assertEqual(contract["global_lattice"]["first_target_index"], 0)
            self.assertEqual(contract["global_lattice"]["last_target_index"], 7)
            self.assertTrue(contract["global_lattice"]["zero_gap_target_indices"])
            self.assertTrue(contract["global_lattice"]["zero_duplicate_target_indices"])
            self.assertTrue(contract["global_lattice"]["zero_duplicate_selected_pts"])
            self.assertEqual(contract["settings"]["research_contract_sha256"], "C" * 64)
            self.assertEqual(
                [section["target_count"] for section in contract["sections"]],
                [3, 5],
            )
            ledger_rows = [
                json.loads(line)
                for line in (output / "target-ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([row["target_index"] for row in ledger_rows], list(range(8)))
            self.assertEqual(ledger_rows[3]["target_fraction"], "3/4")
            self.assertEqual(ledger_rows[3]["selected_source_pts_fraction"], "23/30")
            self.assertEqual(ledger_rows[3]["owner_section_index"], 1)
            self.assertEqual(
                contract,
                json.loads((output / "contract.json").read_text(encoding="utf-8")),
            )

    def test_extracts_all_contract_sections_and_publishes_zero_gap_completion(self):
        module = self.load_module()
        self.assertTrue(
            hasattr(module, "run_all_global_sections"),
            "bounded resumable full-section runner must exist",
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            visual_root = Path(tmp) / "visual-4fps-global-v1"
            self.make_video(source)
            module.write_global_contract(
                source=source,
                output_dir=visual_root,
                plan_rows=[
                    {"index": 0, "start_seconds": "0.000000", "end_seconds": "0.766667"},
                    {"index": 1, "start_seconds": "0.766667", "end_seconds": "2.000000"},
                ],
                baseline_fps=4,
                long_edge=320,
                plan_sha256="B" * 64,
                research_contract_sha256="C" * 64,
            )

            completion = module.run_all_global_sections(
                source=source,
                visual_root=visual_root,
                workers=2,
            )

            self.assertEqual(completion["section_count"], 2)
            self.assertEqual(completion["global_target_count"], 8)
            self.assertEqual(completion["extracted_frame_count"], 8)
            self.assertTrue(completion["zero_gap_target_indices"])
            self.assertTrue(completion["zero_duplicate_target_indices"])
            self.assertTrue(completion["zero_duplicate_source_pts"])
            chunk_zero = json.loads(
                (visual_root / "chunks" / "chunk-00000" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            chunk_one = json.loads(
                (visual_root / "chunks" / "chunk-00001" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("23/30", [row["source_pts_fraction"] for row in chunk_zero["frames"]])
            self.assertIn("23/30", [row["source_pts_fraction"] for row in chunk_one["frames"]])
            self.assertEqual(
                completion,
                json.loads((visual_root / "completion.json").read_text(encoding="utf-8")),
            )

    def test_builds_unpadded_contact_sheets_from_only_completed_global_tree(self):
        module = self.load_module()
        self.assertTrue(
            hasattr(module, "build_global_contact_sheets"),
            "global-only atomic contact-sheet builder must exist",
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            visual_root = Path(tmp) / "visual-4fps-global-v1-r2"
            output = Path(tmp) / "contact-sheets-global-v1-r2"
            self.make_video(source)
            module.write_global_contract(
                source=source,
                output_dir=visual_root,
                plan_rows=[
                    {"index": 0, "start_seconds": "0.000000", "end_seconds": "0.766667"},
                    {"index": 1, "start_seconds": "0.766667", "end_seconds": "2.000000"},
                ],
                baseline_fps=4,
                long_edge=320,
                plan_sha256="D" * 64,
                research_contract_sha256="E" * 64,
            )
            module.run_all_global_sections(
                source=source,
                visual_root=visual_root,
                workers=2,
            )

            manifest = module.build_global_contact_sheets(
                visual_root=visual_root,
                output_dir=output,
                tile_width=160,
                tile_height=90,
            )

            self.assertEqual(manifest["source_tree"], "visual-4fps-global-v1-only")
            self.assertEqual(manifest["view_state"], "generated_unviewed")
            self.assertEqual(manifest["total_frames"], 8)
            self.assertEqual(manifest["total_sheets"], 1)
            self.assertEqual(manifest["padding_cells_total"], 0)
            records = [
                json.loads(line)
                for line in (output / "contact-sheet-index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [row["global_target_index"] for row in records[0]["frames"]],
                list(range(8)),
            )
            self.assertEqual(records[0]["frame_count"], 8)
            self.assertEqual(records[0]["padding_cells"], 0)
            self.assertTrue((output / records[0]["relative_path"]).is_file())
            self.assertEqual(
                manifest,
                json.loads(
                    (output / "contact-sheet-manifest.json").read_text(encoding="utf-8")
                ),
            )

    def test_exact_rational_target_threshold_does_not_round_past_source_frame(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            visual_root = Path(tmp) / "visual-4fps-global-v1"
            self.make_video(source)
            module.write_global_contract(
                source=source,
                output_dir=visual_root,
                plan_rows=[
                    {"index": 0, "start_seconds": "0.000000", "end_seconds": "0.333333"},
                    {"index": 1, "start_seconds": "0.333333", "end_seconds": "2.000000"},
                ],
                baseline_fps=4,
                long_edge=320,
                plan_sha256="F" * 64,
                research_contract_sha256="A" * 64,
            )

            completion = module.run_all_global_sections(
                source=source,
                visual_root=visual_root,
                workers=1,
            )

            second = json.loads(
                (visual_root / "chunks" / "chunk-00001" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(second["frames"][0]["global_target_fraction"], "1/2")
            self.assertEqual(second["frames"][0]["source_pts_fraction"], "1/2")
            self.assertEqual(completion["global_target_count"], 8)


if __name__ == "__main__":
    unittest.main()
