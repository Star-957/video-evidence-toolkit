import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from extract_visual_chunk import (
    _validate_local_output_reference,
    _validate_local_source_reference,
    run_visual_chunk,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class VisualChunkTests(unittest.TestCase):
    def make_video(self, path, *, fps, duration, size="320x180"):
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={size}:rate={fps}:duration={duration}",
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

    def make_gap_video(self, path):
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
                "-vf",
                "setpts='if(lt(N,30),N/(30*TB),(N+30)/(30*TB))'",
                "-fps_mode",
                "vfr",
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

    def make_nonzero_origin_video(self, path):
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
                "-vf",
                "setpts=PTS+5/TB",
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

    def test_nonzero_timestamp_origin_fails_closed_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "nonzero-origin.mkv"
            output = Path(tmp) / "baseline"
            self.make_nonzero_origin_video(source)

            with self.assertRaisesRegex(
                RuntimeError, "Non-zero timestamp origin is unsupported"
            ):
                run_visual_chunk(
                    source,
                    output,
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=2,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=8,
                )
            self.assertFalse(output.exists())

    def test_baseline_four_fps_manifest_has_real_images_and_monotonic_pts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            output = Path(tmp) / "baseline"
            self.make_video(source, fps=30, duration=2)

            manifest = run_visual_chunk(
                source,
                output,
                mode="baseline",
                start_seconds=0,
                duration_seconds=2,
                baseline_fps=4,
                max_native_fps=None,
                burst_trigger=None,
                long_edge=448,
                max_frames=100,
            )

            self.assertEqual(manifest["settings"]["mode"], "baseline")
            self.assertEqual(manifest["pipeline_version"], "visual-chunk-v5")
            self.assertEqual(manifest["settings"]["baseline_fps"], 4)
            self.assertEqual(len(manifest["frames"]), 8)
            self.assertEqual(
                {(frame["width"], frame["height"]) for frame in manifest["frames"]},
                {(320, 180)},
            )
            timestamps = [frame["source_pts_seconds"] for frame in manifest["frames"]]
            self.assertEqual(timestamps, sorted(timestamps))
            self.assertEqual(len(timestamps), len(set(timestamps)))
            self.assertEqual(
                [frame["sample_target_seconds"] for frame in manifest["frames"]],
                [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75],
            )
            self.assertTrue(
                all(
                    0 <= frame["sampling_error_seconds"] <= (1 / 30) + 0.001
                    for frame in manifest["frames"]
                )
            )
            self.assertEqual(
                manifest,
                json.loads((output / "manifest.json").read_text(encoding="utf-8")),
            )
            for frame in manifest["frames"]:
                image = output / frame["relative_path"]
                self.assertTrue(image.is_file())
                self.assertGreater(image.stat().st_size, 0)
                self.assertEqual(len(frame["sha256"]), 64)

    def test_native_sixty_fps_burst_preserves_native_frame_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source60.mkv"
            output = Path(tmp) / "burst"
            self.make_video(source, fps=60, duration=1)

            manifest = run_visual_chunk(
                source,
                output,
                mode="native-burst",
                start_seconds=0,
                duration_seconds=1,
                baseline_fps=None,
                max_native_fps=60,
                burst_trigger="synthetic reaction-latency fixture",
                long_edge=448,
                max_frames=100,
            )

            self.assertEqual(manifest["settings"]["mode"], "native-burst")
            self.assertEqual(manifest["settings"]["max_native_fps"], 60)
            self.assertEqual(len(manifest["frames"]), 60)
            self.assertTrue(
                all(frame["sample_target_seconds"] is None for frame in manifest["frames"])
            )
            self.assertTrue(
                all(frame["sampling_error_seconds"] is None for frame in manifest["frames"])
            )
            self.assertEqual(manifest["burst_summary"]["distinct_pts_count"], 60)
            self.assertEqual(manifest["burst_summary"]["duplicate_pts_count"], 0)
            self.assertEqual(manifest["burst_summary"]["pipeline_drop_count"], 0)
            self.assertEqual(
                len({frame["source_pts_seconds"] for frame in manifest["frames"]}),
                60,
            )

    def test_native_one_hundred_twenty_fps_burst_is_supported_without_interpolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source120.mkv"
            output = Path(tmp) / "burst120"
            self.make_video(source, fps=120, duration=1)

            manifest = run_visual_chunk(
                source,
                output,
                mode="native-burst",
                start_seconds=0,
                duration_seconds=1,
                baseline_fps=None,
                max_native_fps=120,
                burst_trigger="synthetic fast-expression fixture",
                long_edge=448,
                max_frames=120,
            )

            self.assertEqual(len(manifest["frames"]), 120)
            self.assertFalse(manifest["settings"]["interpolation_allowed"])
            self.assertEqual(
                len({frame["source_pts_seconds"] for frame in manifest["frames"]}),
                120,
            )

    def test_native_burst_rejects_source_above_declared_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source120.mkv"
            output = Path(tmp) / "burst"
            self.make_video(source, fps=120, duration=1)

            with self.assertRaisesRegex(ValueError, "native FPS exceeds"):
                run_visual_chunk(
                    source,
                    output,
                    mode="native-burst",
                    start_seconds=0,
                    duration_seconds=1,
                    baseline_fps=None,
                    max_native_fps=60,
                    burst_trigger="ceiling negative control",
                    long_edge=448,
                    max_frames=200,
                )
            self.assertFalse(output.exists())

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            output = Path(tmp) / "existing"
            self.make_video(source, fps=30, duration=1)
            output.mkdir()
            marker = output / "marker.txt"
            marker.write_text("preserve", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_visual_chunk(
                    source,
                    output,
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=1,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=100,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_interior_ranges_work_for_baseline_and_native_bursts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source30 = Path(tmp) / "source30.mkv"
            source60 = Path(tmp) / "source60.mkv"
            source120 = Path(tmp) / "source120.mkv"
            self.make_video(source30, fps=30, duration=3)
            self.make_video(source60, fps=60, duration=3)
            self.make_video(source120, fps=120, duration=3)

            baseline = run_visual_chunk(
                source30,
                Path(tmp) / "baseline",
                mode="baseline",
                start_seconds=1,
                duration_seconds=1,
                baseline_fps=4,
                max_native_fps=None,
                burst_trigger=None,
                long_edge=448,
                max_frames=4,
            )
            burst60 = run_visual_chunk(
                source60,
                Path(tmp) / "burst60",
                mode="native-burst",
                start_seconds=1,
                duration_seconds=1,
                baseline_fps=None,
                max_native_fps=60,
                burst_trigger="interior 60 fps fixture",
                long_edge=448,
                max_frames=60,
            )
            burst120 = run_visual_chunk(
                source120,
                Path(tmp) / "burst120",
                mode="native-burst",
                start_seconds=1,
                duration_seconds=1,
                baseline_fps=None,
                max_native_fps=120,
                burst_trigger="interior 120 fps fixture",
                long_edge=448,
                max_frames=120,
            )

            self.assertEqual(len(baseline["frames"]), 4)
            self.assertEqual(len(burst60["frames"]), 60)
            self.assertEqual(len(burst120["frames"]), 120)
            self.assertGreaterEqual(baseline["frames"][0]["source_pts_seconds"], 1)
            self.assertLess(baseline["frames"][-1]["source_pts_seconds"], 2)

    def test_low_fps_and_vfr_gap_fail_closed_for_four_fps_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            low = Path(tmp) / "low.mkv"
            gap = Path(tmp) / "gap.mkv"
            self.make_video(low, fps=2, duration=2)
            self.make_gap_video(gap)

            with self.assertRaisesRegex(ValueError, "below requested baseline"):
                run_visual_chunk(
                    low,
                    Path(tmp) / "low-output",
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=2,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=8,
                )
            with self.assertRaisesRegex(RuntimeError, "coverage gate"):
                run_visual_chunk(
                    gap,
                    Path(tmp) / "gap-output",
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=3,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=12,
                )

    def test_playlist_and_unc_inputs_are_rejected_before_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "source.ffconcat"
            playlist.write_text("ffconcat version 1.0\nfile child.mkv\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "single media-file"):
                run_visual_chunk(
                    playlist,
                    Path(tmp) / "playlist-output",
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=1,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=4,
                )
            disguised = Path(tmp) / "renamed-playlist.mp4"
            disguised.write_text(
                "ffconcat version 1.0\nfile child.mkv\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
                run_visual_chunk(
                    disguised,
                    Path(tmp) / "disguised-output",
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=1,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=4,
                )
            self.assertFalse((Path(tmp) / "disguised-output").exists())
            with self.assertRaisesRegex(ValueError, "network or UNC"):
                run_visual_chunk(
                    Path(r"\\server\share\source.mp4"),
                    Path(tmp) / "unc-output",
                    mode="baseline",
                    start_seconds=0,
                    duration_seconds=1,
                    baseline_fps=4,
                    max_native_fps=None,
                    burst_trigger=None,
                    long_edge=448,
                    max_frames=4,
                )

    def test_resolved_unc_target_is_rejected(self):
        with patch(
            "extract_visual_chunk.Path.resolve",
            return_value=Path(r"\\server\share\source.mp4"),
        ):
            with self.assertRaisesRegex(ValueError, "network or UNC"):
                _validate_local_source_reference(Path(r"C:\local\source.mp4"))

    def test_raw_resolved_and_mapped_network_outputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "network or UNC"):
            _validate_local_output_reference(Path(r"\\server\share\frames"))

        with patch(
            "extract_visual_chunk.Path.resolve",
            return_value=Path(r"\\server\share\frames"),
        ):
            with self.assertRaisesRegex(ValueError, "network or UNC"):
                _validate_local_output_reference(Path(r"C:\local\frames"))

        with patch("extract_visual_chunk._get_drive_type", return_value=4):
            with self.assertRaisesRegex(ValueError, "Mapped network"):
                _validate_local_output_reference(Path(r"Z:\frames"))

    def test_source_mutation_fails_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            output = Path(tmp) / "output"
            self.make_video(source, fps=30, duration=1)
            from extract_visual_chunk import _sha256 as real_sha256

            source_hash_calls = 0

            def changing_hash(path):
                nonlocal source_hash_calls
                if Path(path) == source.resolve():
                    source_hash_calls += 1
                    return ("A" if source_hash_calls == 1 else "B") * 64
                return real_sha256(path)

            with patch("extract_visual_chunk._sha256", changing_hash):
                with self.assertRaisesRegex(RuntimeError, "changed during extraction"):
                    run_visual_chunk(
                        source,
                        output,
                        mode="baseline",
                        start_seconds=0,
                        duration_seconds=1,
                        baseline_fps=4,
                        max_native_fps=None,
                        burst_trigger=None,
                        long_edge=448,
                        max_frames=4,
                    )
            self.assertFalse(output.exists())

    def test_tampered_staged_manifest_and_missing_frame_never_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            output = Path(tmp) / "output"
            self.make_video(source, fps=30, duration=1)
            real_write_text = Path.write_text

            def corrupt_manifest(path, data, *args, **kwargs):
                if path.name == "manifest.json":
                    payload = json.loads(data)
                    payload["schema_version"] = 999
                    payload["frames"][0]["relative_path"] = "frames/missing.jpg"
                    data = json.dumps(payload)
                return real_write_text(path, data, *args, **kwargs)

            with patch("extract_visual_chunk.Path.write_text", corrupt_manifest):
                with self.assertRaisesRegex(RuntimeError, "Manifest schema/hash validation"):
                    run_visual_chunk(
                        source,
                        output,
                        mode="baseline",
                        start_seconds=0,
                        duration_seconds=1,
                        baseline_fps=4,
                        max_native_fps=None,
                        burst_trigger=None,
                        long_edge=448,
                        max_frames=4,
                    )
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).glob(".output.tmp-*")), [])

            extra_output = Path(tmp) / "extra-output"

            def inject_unmanifested_file(path, data, *args, **kwargs):
                result = real_write_text(path, data, *args, **kwargs)
                if path.name == "manifest.json":
                    (path.parent / "frames" / "unmanifested.jpg").write_bytes(b"rogue")
                return result

            with patch(
                "extract_visual_chunk.Path.write_text", inject_unmanifested_file
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Manifest schema/hash validation"
                ):
                    run_visual_chunk(
                        source,
                        extra_output,
                        mode="baseline",
                        start_seconds=0,
                        duration_seconds=1,
                        baseline_fps=4,
                        max_native_fps=None,
                        burst_trigger=None,
                        long_edge=448,
                        max_frames=4,
                    )
            self.assertFalse(extra_output.exists())
            self.assertEqual(list(Path(tmp).glob(".extra-output.tmp-*")), [])

    def test_late_created_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            output = Path(tmp) / "race"
            self.make_video(source, fps=30, duration=1)
            real_rename = __import__("os").rename

            def create_destination_then_rename(source_path, destination):
                destination = Path(destination)
                destination.mkdir()
                (destination / "marker.txt").write_text("preserve", encoding="utf-8")
                return real_rename(source_path, destination)

            with patch("extract_visual_chunk.os.rename", create_destination_then_rename):
                with self.assertRaises((FileExistsError, OSError)):
                    run_visual_chunk(
                        source,
                        output,
                        mode="baseline",
                        start_seconds=0,
                        duration_seconds=1,
                        baseline_fps=4,
                        max_native_fps=None,
                        burst_trigger=None,
                        long_edge=448,
                        max_frames=4,
                    )
            self.assertEqual((output / "marker.txt").read_text("utf-8"), "preserve")
            self.assertEqual(list(Path(tmp).glob(".race.tmp-*")), [])

    def test_landscape_portrait_and_small_sources_never_upscale(self):
        cases = [
            ("small.mkv", "320x180", (320, 180)),
            ("landscape.mkv", "1920x1080", (448, 252)),
            ("portrait.mkv", "720x1280", (252, 448)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for name, source_size, expected_size in cases:
                with self.subTest(source_size=source_size):
                    source = Path(tmp) / name
                    output = Path(tmp) / f"out-{name}"
                    self.make_video(source, fps=30, duration=0.25, size=source_size)
                    manifest = run_visual_chunk(
                        source,
                        output,
                        mode="baseline",
                        start_seconds=0,
                        duration_seconds=0.25,
                        baseline_fps=4,
                        max_native_fps=None,
                        burst_trigger=None,
                        long_edge=448,
                        max_frames=1,
                    )
                    self.assertEqual(
                        (manifest["frames"][0]["width"], manifest["frames"][0]["height"]),
                        expected_size,
                    )


if __name__ == "__main__":
    unittest.main()
