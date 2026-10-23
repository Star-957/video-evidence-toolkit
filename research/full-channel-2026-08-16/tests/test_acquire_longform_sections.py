import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from acquire_longform_sections import (
    acquire_full_source,
    acquire_section,
    build_ytdlp_full_command,
    build_ytdlp_section_command,
    build_sections,
    initialize_contract,
    main,
    sanitize_error,
    validate_local_path,
    verify_contract,
    verify_segment,
)


class LongformPlanningTests(unittest.TestCase):
    def identity(self):
        return {
            "video_id": "exact123",
            "title": "Exact item",
            "uploader": "Official channel",
            "duration_seconds": "1201.250000",
            "availability": "public",
            "is_live": False,
        }

    def test_build_sections_is_contiguous_end_exclusive_and_bounded(self):
        sections = build_sections(Decimal("1201.25"), Decimal("600"))

        self.assertEqual(
            [(row["start_seconds"], row["end_seconds"]) for row in sections],
            [
                ("0.000000", "600.000000"),
                ("600.000000", "1200.000000"),
                ("1200.000000", "1201.250000"),
            ],
        )
        self.assertEqual([row["index"] for row in sections], [0, 1, 2])
        self.assertTrue(
            all(Decimal(row["duration_seconds"]) <= 600 for row in sections)
        )
        self.assertEqual(
            [row["directory"] for row in sections],
            [
                "segments/segment-00000",
                "segments/segment-00001",
                "segments/segment-00002",
            ],
        )

    def test_build_sections_rejects_invalid_duration_or_cap(self):
        for duration, cap in (("0", "600"), ("1", "0"), ("1", "600.000001")):
            with self.subTest(duration=duration, cap=cap):
                with self.assertRaises(ValueError):
                    build_sections(Decimal(duration), Decimal(cap))

    def test_validate_local_path_rejects_raw_unc(self):
        with self.assertRaises(ValueError):
            validate_local_path(Path(r"\\server\share\attempt"), must_exist=False)

    def test_initialize_contract_binds_exact_url_identity_and_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            url_file = base / "url.txt"
            exact_url = "https://www.youtube.com/watch?v=exact123"
            url_file.write_text(exact_url + "\n", encoding="utf-8")
            output = base / "source-segments"

            contract = initialize_contract(
                url_file,
                output,
                self.identity(),
                max_section_seconds=Decimal("600"),
            )

            self.assertEqual(contract["schema"], "longform-source-segments-v1")
            self.assertEqual(contract["source"]["url"], exact_url)
            self.assertEqual(
                contract["source"]["url_sha256"],
                hashlib.sha256(exact_url.encode("utf-8")).hexdigest().upper(),
            )
            self.assertEqual(contract["source"]["video_id"], "exact123")
            self.assertEqual(contract["settings"]["max_section_seconds"], "600.000000")
            self.assertEqual(
                contract["settings"]["source_format_selector"],
                "18/b[height<=360]",
            )
            self.assertEqual(
                contract["pipeline_provenance"]["script_sha256"],
                hashlib.sha256(
                    (SCRIPTS_DIR / "acquire_longform_sections.py").read_bytes()
                ).hexdigest().upper(),
            )
            self.assertEqual(
                len(contract["pipeline_provenance"]["policy_sha256"]), 64
            )
            self.assertEqual(len(contract["sections"]), 3)
            self.assertTrue((output / "segments").is_dir())
            self.assertEqual(
                contract,
                json.loads((output / "contract.json").read_text(encoding="utf-8")),
            )

    def test_initialize_contract_no_clobber_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            url_file = base / "url.txt"
            url_file.write_text(
                "https://www.youtube.com/watch?v=exact123\n", encoding="utf-8"
            )
            output = base / "source-segments"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                initialize_contract(
                    url_file,
                    output,
                    self.identity(),
                    max_section_seconds=Decimal("600"),
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse((output / "contract.json").exists())

    def test_initialize_contract_rejects_url_video_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            url_file = base / "url.txt"
            url_file.write_text(
                "https://www.youtube.com/watch?v=different1\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "video ID"):
                initialize_contract(
                    url_file,
                    base / "source-segments",
                    self.identity(),
                    max_section_seconds=Decimal("600"),
                )

    def test_init_cli_consumes_attempt_owned_identity_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            url_file = base / "url.txt"
            url_file.write_text(
                "https://www.youtube.com/watch?v=exact123\n", encoding="utf-8"
            )
            identity_file = base / "identity.json"
            identity_file.write_text(
                json.dumps(self.identity()), encoding="utf-8"
            )
            output = base / "source-segments"

            result = main(
                [
                    "init",
                    "--url-file",
                    str(url_file),
                    "--identity-file",
                    str(identity_file),
                    "--output-dir",
                    str(output),
                    "--max-section-seconds",
                    "600",
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((output / "contract.json").is_file())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class LongformSegmentTests(unittest.TestCase):
    def make_video(self, path: Path, duration: str = "2") -> None:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=160x90:rate=30:duration={duration}",
                "-an",
                "-c:v",
                "ffv1",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def make_contract(self, base: Path, duration: str = "2") -> Path:
        url_file = base / "url.txt"
        url_file.write_text(
            "https://www.youtube.com/watch?v=exact123\n", encoding="utf-8"
        )
        output = base / "source-segments"
        initialize_contract(
            url_file,
            output,
            {
                "video_id": "exact123",
                "title": "Exact item",
                "uploader": "Official channel",
                "duration_seconds": duration,
                "availability": "public",
                "is_live": False,
            },
            max_section_seconds=Decimal("600"),
        )
        return output

    def copy_runner(self, fixture: Path):
        def run(_url, _section, staging):
            shutil.copy2(fixture, staging / "media.mkv")

        return run

    def test_full_source_acquisition_is_hashed_probed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "fixture.mkv"
            self.make_video(fixture)
            root = self.make_contract(base)

            first = acquire_full_source(root, runner=self.copy_runner(fixture))

            def forbidden(*_args):
                self.fail("runner must not run for verified full-source resume")

            second = acquire_full_source(root, runner=forbidden)
            self.assertEqual(first, second)
            media = root / "full-source" / "media.mkv"
            self.assertTrue(media.is_file())
            self.assertEqual(first["media"]["sha256"], hashlib.sha256(media.read_bytes()).hexdigest().upper())
            self.assertEqual(first["source"]["video_id"], "exact123")

    def test_full_source_command_is_one_exact_native_bounded_item(self):
        command = build_ytdlp_full_command("yt-dlp.exe")
        self.assertIn("--no-playlist", command)
        self.assertEqual(command[command.index("--playlist-end") + 1], "1")
        self.assertEqual(command[command.index("--format") + 1], "18/b[height<=360]")
        self.assertEqual(
            command[command.index("--http-chunk-size") + 1], "256K"
        )
        self.assertNotIn("--download-sections", command)
        self.assertNotIn("--force-keyframes-at-cuts", command)
        self.assertNotIn("--extractor-args", command)
        self.assertEqual(command[-2:], ["--batch-file", "-"])

    def test_default_section_runner_stream_copies_verified_full_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            full_fixture = base / "full.mkv"
            self.make_video(full_fixture, duration="2")
            url_file = base / "url.txt"
            url_file.write_text(
                "https://www.youtube.com/watch?v=exact123\n", encoding="utf-8"
            )
            root = base / "source-segments"
            initialize_contract(
                url_file,
                root,
                {
                    "video_id": "exact123",
                    "title": "Exact item",
                    "uploader": "Official channel",
                    "duration_seconds": "2",
                    "availability": "public",
                    "is_live": False,
                },
                max_section_seconds=Decimal("1"),
            )
            acquire_full_source(root, runner=self.copy_runner(full_fixture))

            manifest = acquire_section(root, 0)

            self.assertEqual(
                manifest["acquisition"]["runner"],
                "ffmpeg-local-stream-copy-v1",
            )
            self.assertEqual(
                manifest["source"]["full_source_sha256"],
                hashlib.sha256(full_fixture.read_bytes()).hexdigest().upper(),
            )
            self.assertEqual(manifest["media"]["ffprobe"]["duration_seconds"], "1.000000")

    def test_acquire_section_probes_hashes_and_atomically_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "fixture.mkv"
            self.make_video(fixture)
            root = self.make_contract(base)

            manifest = acquire_section(root, 0, runner=self.copy_runner(fixture))

            segment = root / "segments" / "segment-00000"
            media = segment / "media.mkv"
            self.assertTrue(media.is_file())
            self.assertEqual(manifest["schema"], "longform-source-segment-v1")
            self.assertEqual(manifest["source"]["video_id"], "exact123")
            self.assertEqual(manifest["section"]["start_seconds"], "0.000000")
            self.assertEqual(manifest["section"]["end_seconds"], "2.000000")
            self.assertEqual(manifest["media"]["sha256"], hashlib.sha256(media.read_bytes()).hexdigest().upper())
            self.assertEqual(manifest["media"]["bytes"], media.stat().st_size)
            self.assertEqual(manifest, verify_segment(root, 0))

    def test_existing_valid_segment_is_idempotent_without_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "fixture.mkv"
            self.make_video(fixture)
            root = self.make_contract(base)
            first = acquire_section(root, 0, runner=self.copy_runner(fixture))

            def forbidden(*_args):
                self.fail("runner must not be called for a valid resumed segment")

            second = acquire_section(root, 0, runner=forbidden)
            self.assertEqual(first, second)

    def test_tampered_existing_segment_fails_closed_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "fixture.mkv"
            self.make_video(fixture)
            root = self.make_contract(base)
            acquire_section(root, 0, runner=self.copy_runner(fixture))
            media = root / "segments" / "segment-00000" / "media.mkv"
            with media.open("ab") as handle:
                handle.write(b"tamper")

            with self.assertRaisesRegex(RuntimeError, "existing segment is invalid"):
                acquire_section(root, 0, runner=lambda *_: self.fail("no replacement"))
            self.assertTrue(media.read_bytes().endswith(b"tamper"))

    def test_extra_file_fails_exact_set_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "fixture.mkv"
            self.make_video(fixture)
            root = self.make_contract(base)
            acquire_section(root, 0, runner=self.copy_runner(fixture))
            extra = root / "segments" / "segment-00000" / "extra.txt"
            extra.write_text("unexpected", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "file set"):
                verify_segment(root, 0)

    def test_runner_failure_cleans_staging_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.make_contract(base)

            def fail_runner(_url, _section, staging):
                (staging / "partial.bin").write_bytes(b"partial")
                raise RuntimeError("synthetic acquisition failure")

            with self.assertRaisesRegex(RuntimeError, "synthetic acquisition failure"):
                acquire_section(root, 0, runner=fail_runner)

            self.assertFalse((root / "segments" / "segment-00000").exists())
            self.assertEqual(
                list((root / "segments").glob(".segment-00000.partial-*")), []
            )

    def test_wrong_duration_media_fails_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "short.mkv"
            self.make_video(fixture, duration="1")
            root = self.make_contract(base, duration="2")

            with self.assertRaisesRegex(RuntimeError, "duration"):
                acquire_section(root, 0, runner=self.copy_runner(fixture))

            self.assertFalse((root / "segments" / "segment-00000").exists())

    def test_ytdlp_command_is_exact_item_bounded_and_has_no_bypass_flags(self):
        command = build_ytdlp_section_command(
            "yt-dlp.exe",
            {
                "start_seconds": "600.000000",
                "end_seconds": "1200.000000",
            },
        )

        self.assertIn("--no-playlist", command)
        self.assertEqual(command[command.index("--playlist-end") + 1], "1")
        self.assertEqual(
            command[command.index("--download-sections") + 1],
            "*600.000000-1200.000000",
        )
        self.assertEqual(
            command[command.index("--format") + 1], "18/b[height<=360]"
        )
        self.assertIn("--no-remote-components", command)
        self.assertEqual(
            command[command.index("--extractor-args") + 1],
            "youtube:player_client=tv_embedded",
        )
        forbidden = {
            "--cookies",
            "--cookies-from-browser",
            "--username",
            "--password",
            "--proxy",
            "--impersonate",
            "--force-keyframes-at-cuts",
            "--yes-playlist",
        }
        self.assertFalse(forbidden.intersection(command))

    def test_error_sanitizer_redacts_urls_and_bounds_receipt(self):
        raw = ("prefix " * 300) + "https://private.example/video?token=secret final"
        value = sanitize_error(raw)
        self.assertNotIn("private.example", value)
        self.assertNotIn("secret", value)
        self.assertIn("[URL]", value)
        self.assertLessEqual(len(value), 1000)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class LongformCompletionTests(LongformSegmentTests):
    def make_two_segment_contract(self, base: Path) -> tuple[Path, Path]:
        fixture = base / "one-second.mkv"
        self.make_video(fixture, duration="1")
        url_file = base / "url.txt"
        url_file.write_text(
            "https://www.youtube.com/watch?v=exact123\n", encoding="utf-8"
        )
        root = base / "source-segments"
        initialize_contract(
            url_file,
            root,
            {
                "video_id": "exact123",
                "title": "Exact item",
                "uploader": "Official channel",
                "duration_seconds": "2",
                "availability": "public",
                "is_live": False,
            },
            max_section_seconds=Decimal("1"),
        )
        return root, fixture

    def test_whole_item_gate_requires_every_planned_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, fixture = self.make_two_segment_contract(base)
            acquire_section(root, 0, runner=self.copy_runner(fixture))

            with self.assertRaisesRegex(RuntimeError, "missing planned segment"):
                verify_contract(root, publish_complete=True)

            self.assertFalse((root / "whole-item-complete.json").exists())

    def test_whole_item_gate_rejects_extra_segment_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, fixture = self.make_two_segment_contract(base)
            acquire_section(root, 0, runner=self.copy_runner(fixture))
            acquire_section(root, 1, runner=self.copy_runner(fixture))
            (root / "segments" / "segment-99999").mkdir()

            with self.assertRaisesRegex(RuntimeError, "segment directory set"):
                verify_contract(root, publish_complete=True)

    def test_contract_gap_or_overlap_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, _fixture = self.make_two_segment_contract(base)
            path = root / "contract.json"
            contract = json.loads(path.read_text(encoding="utf-8"))
            contract["sections"][1]["start_seconds"] = "0.900000"
            path.write_text(json.dumps(contract), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "gap or overlap"):
                verify_contract(root, publish_complete=True)

    def test_whole_item_completion_is_hashed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, fixture = self.make_two_segment_contract(base)
            acquire_section(root, 0, runner=self.copy_runner(fixture))
            acquire_section(root, 1, runner=self.copy_runner(fixture))

            first = verify_contract(root, publish_complete=True)
            second = verify_contract(root, publish_complete=True)

            self.assertEqual(first, second)
            self.assertEqual(first["schema"], "longform-source-complete-v1")
            self.assertEqual(first["segment_count"], 2)
            self.assertEqual(first["coverage"], {
                "start_seconds": "0.000000",
                "end_seconds": "2.000000",
                "gap_seconds": "0.000000",
                "overlap_seconds": "0.000000",
            })
            self.assertEqual(len(first["segments_tree_sha256"]), 64)
            self.assertEqual(
                first,
                json.loads((root / "whole-item-complete.json").read_text(encoding="utf-8")),
            )
