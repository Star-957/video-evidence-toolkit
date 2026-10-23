import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
SCRIPT = SCRIPTS_DIR / "youtube_inventory.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from youtube_inventory import YouTubeDataAPI, run_cli, run_raw_channel


class FakeYouTubeAPI:
    def __init__(self):
        self.calls = []

    def get(self, resource, params):
        self.calls.append((resource, dict(params)))
        if resource == "channels":
            return {
                "kind": "youtube#channelListResponse",
                "items": [
                    {
                        "id": "UC_TEST",
                        "snippet": {"title": "Test channel"},
                        "contentDetails": {
                            "relatedPlaylists": {"uploads": "UU_TEST"}
                        },
                    }
                ],
            }
        if resource == "playlistItems":
            if not params.get("pageToken"):
                return {
                    "kind": "youtube#playlistItemListResponse",
                    "items": [
                        {"contentDetails": {"videoId": "video000001"}},
                        {"contentDetails": {"videoId": "video000002"}},
                    ],
                    "nextPageToken": "NEXT",
                }
            return {
                "kind": "youtube#playlistItemListResponse",
                "items": [
                    {"contentDetails": {"videoId": "video000003"}}
                ],
            }
        if resource == "videos":
            return {
                "kind": "youtube#videoListResponse",
                "items": [
                    {"id": video_id, "contentDetails": {"duration": "PT1M"}}
                    for video_id in params["id"].split(",")
                ],
            }
        raise AssertionError(f"Unexpected resource: {resource}")


class RawChannelCollectorTests(unittest.TestCase):
    def write_approved_manifest(self, path, reference="test-approval"):
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "approved": True,
                    "issuer": "YouTube compliance test fixture",
                    "scope": ["one_channel_full_uploads_raw_api_receipts"],
                    "reference": reference,
                    "approved_at": "2026-08-16T00:00:00Z",
                    "approval_document_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )

    def test_http_key_is_header_only_and_not_exposed(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"] = self.path
                seen["key_header"] = self.headers.get("X-Goog-Api-Key")
                body = b'{"items":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = YouTubeDataAPI(
                "test-secret-key",
                base_url=f"http://127.0.0.1:{server.server_port}/youtube/v3",
            )
            result = client.get("channels", {"part": "id", "id": "UC_TEST"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result, {"items": []})
        self.assertEqual(seen["key_header"], "test-secret-key")
        self.assertNotIn("key", parse_qs(urlparse(seen["path"]).query))
        self.assertNotIn("test-secret-key", repr(client))

    def test_redirect_fails_closed_and_never_forwards_key(self):
        seen = {"redirect_key": None, "target_called": False, "target_key": None}

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["target_called"] = True
                seen["target_key"] = self.headers.get("X-Goog-Api-Key")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["redirect_key"] = self.headers.get("X-Goog-Api-Key")
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target.server_port}/capture",
                )
                self.end_headers()

            def log_message(self, *_args):
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [
            threading.Thread(target=target.serve_forever, daemon=True),
            threading.Thread(target=redirect.serve_forever, daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            client = YouTubeDataAPI(
                "redirect-secret",
                base_url=f"http://127.0.0.1:{redirect.server_port}/youtube/v3",
            )
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                client.get("channels", {"part": "id", "id": "UC_TEST"})
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(seen["redirect_key"], "redirect-secret")
        self.assertFalse(seen["target_called"])
        self.assertIsNone(seen["target_key"])

    def test_one_channel_run_preserves_raw_responses_without_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "UC_TEST-20260816"
            receipt = run_raw_channel(
                FakeYouTubeAPI(),
                "@test",
                output,
                now=datetime(2026, 8, 16, tzinfo=timezone.utc),
                approval_reference="test-approval",
            )

            self.assertTrue(output.is_dir())
            self.assertEqual(receipt["requested_ref"], "@test")
            self.assertEqual(receipt["delete_or_refresh_due_at"], "2026-09-14T00:00:00Z")
            self.assertEqual(
                json.loads((output / "channel-response.json").read_text("utf-8")),
                {
                    "kind": "youtube#channelListResponse",
                    "items": [
                        {
                            "id": "UC_TEST",
                            "snippet": {"title": "Test channel"},
                            "contentDetails": {
                                "relatedPlaylists": {"uploads": "UU_TEST"}
                            },
                        }
                    ],
                },
            )
            self.assertTrue((output / "playlist-page-0001.json").exists())
            self.assertTrue((output / "playlist-page-0002.json").exists())
            self.assertTrue((output / "video-batch-0001.json").exists())
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "channel-response.json",
                    "playlist-page-0001.json",
                    "playlist-page-0002.json",
                    "video-batch-0001.json",
                    "receipt.json",
                },
            )
            self.assertEqual(
                json.loads((output / "receipt.json").read_text("utf-8")),
                {
                    "api_service": "YouTube Data API v3",
                    "approval_reference": "test-approval",
                    "delete_or_refresh_due_at": "2026-09-14T00:00:00Z",
                    "policy_mode": "one_channel_raw_only_no_derived_data",
                    "requested_ref": "@test",
                    "retrieved_at": "2026-08-16T00:00:00Z",
                    "schema_version": 3,
                    "storage_class": "approval_gated_raw_api_data",
                },
            )
            self.assertEqual(
                json.loads((output / "playlist-page-0001.json").read_text("utf-8")),
                {
                    "kind": "youtube#playlistItemListResponse",
                    "items": [
                        {"contentDetails": {"videoId": "video000001"}},
                        {"contentDetails": {"videoId": "video000002"}},
                    ],
                    "nextPageToken": "NEXT",
                },
            )
            self.assertEqual(
                json.loads((output / "playlist-page-0002.json").read_text("utf-8")),
                {
                    "kind": "youtube#playlistItemListResponse",
                    "items": [
                        {"contentDetails": {"videoId": "video000003"}}
                    ],
                },
            )
            self.assertEqual(
                json.loads((output / "video-batch-0001.json").read_text("utf-8")),
                {
                    "kind": "youtube#videoListResponse",
                    "items": [
                        {"id": "video000001", "contentDetails": {"duration": "PT1M"}},
                        {"id": "video000002", "contentDetails": {"duration": "PT1M"}},
                        {"id": "video000003", "contentDetails": {"duration": "PT1M"}},
                    ],
                },
            )

    def test_existing_output_fails_closed_without_partial_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "existing"
            output.mkdir()
            marker = output / "marker.txt"
            marker.write_text("preserve", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_raw_channel(
                    FakeYouTubeAPI(),
                    "@test",
                    output,
                    now=datetime(2026, 8, 16, tzinfo=timezone.utc),
                    approval_reference="test-approval",
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_mid_run_failure_leaves_no_output_or_attempt_directory(self):
        class FailingAPI(FakeYouTubeAPI):
            def get(self, resource, params):
                if resource == "videos":
                    raise RuntimeError("simulated failure")
                return super().get(resource, params)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "failed"
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                run_raw_channel(
                    FailingAPI(),
                    "@test",
                    output,
                    now=datetime(2026, 8, 16, tzinfo=timezone.utc),
                    approval_reference="test-approval",
                )

            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).glob(".failed.tmp-*")), [])

    def test_multi_channel_reference_is_rejected_before_api_call(self):
        api = FakeYouTubeAPI()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "invalid"
            with self.assertRaisesRegex(ValueError, "single canonical"):
                run_raw_channel(
                    api,
                    "UCAAAAAAAAAAAAAAAAAAAAAA,UCBBBBBBBBBBBBBBBBBBBBBB",
                    output,
                    now=datetime(2026, 8, 16, tzinfo=timezone.utc),
                    approval_reference="test-approval",
                )
        self.assertEqual(api.calls, [])

    def test_late_created_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "race"
            real_rename = __import__("os").rename

            def create_destination_then_rename(source, destination):
                destination = Path(destination)
                destination.mkdir()
                (destination / "marker.txt").write_text("preserve", encoding="utf-8")
                return real_rename(source, destination)

            with patch("youtube_inventory.os.rename", create_destination_then_rename):
                with self.assertRaises((FileExistsError, OSError)):
                    run_raw_channel(
                        FakeYouTubeAPI(),
                        "@test",
                        output,
                        now=datetime(2026, 8, 16, tzinfo=timezone.utc),
                        approval_reference="test-approval",
                    )

            self.assertEqual(
                (output / "marker.txt").read_text(encoding="utf-8"), "preserve"
            )
            self.assertEqual(list(Path(tmp).glob(".race.tmp-*")), [])

    def test_cli_requires_exactly_one_channel_and_uses_environment_key(self):
        captured = {}

        def api_factory(key):
            captured["key"] = key
            return FakeYouTubeAPI()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "raw"
            approval_manifest = Path(tmp) / "approval.json"
            self.write_approved_manifest(approval_manifest)
            run_cli(
                ["--output-dir", str(output), "--channel", "@test"],
                environ={
                    "YOUTUBE_DATA_API_KEY": "secret-for-test",
                    "YOUTUBE_DATA_API_COLLECTION_APPROVAL_REF": "test-approval",
                },
                now=datetime(2026, 8, 16, tzinfo=timezone.utc),
                api_factory=api_factory,
                approval_manifest_path=approval_manifest,
            )
            self.assertEqual(captured["key"], "secret-for-test")

    def test_cli_rejects_duplicate_channel_before_constructing_api(self):
        constructed = []

        def api_factory(key):
            constructed.append(key)
            return FakeYouTubeAPI()

        with self.assertRaises(SystemExit):
            run_cli(
                [
                    "--output-dir",
                    "unused",
                    "--channel",
                    "@first",
                    "--channel",
                    "@second",
                ],
                environ={
                    "YOUTUBE_DATA_API_KEY": "secret-for-test",
                    "YOUTUBE_DATA_API_COLLECTION_APPROVAL_REF": "test-approval",
                },
                api_factory=api_factory,
                approval_manifest_path=Path("unused-approval.json"),
            )
        self.assertEqual(constructed, [])

    def test_cli_requires_documented_collection_approval(self):
        with self.assertRaisesRegex(ValueError, "COLLECTION_APPROVAL_REF"):
            run_cli(
                ["--output-dir", "unused", "--channel", "@test"],
                environ={"YOUTUBE_DATA_API_KEY": "secret-for-test"},
                api_factory=lambda _key: self.fail("API must not be constructed"),
                approval_manifest_path=Path("unused-approval.json"),
            )

    def test_cli_blocks_when_authoritative_manifest_is_not_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "approval.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "approved": False,
                        "issuer": None,
                        "scope": [],
                        "reference": None,
                        "approved_at": None,
                        "approval_document_sha256": None,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not approved"):
                run_cli(
                    ["--output-dir", str(Path(tmp) / "unused"), "--channel", "@test"],
                    environ={
                        "YOUTUBE_DATA_API_KEY": "secret-for-test",
                        "YOUTUBE_DATA_API_COLLECTION_APPROVAL_REF": "x",
                    },
                    api_factory=lambda _key: self.fail("API must not be constructed"),
                    approval_manifest_path=manifest,
                )

    def test_script_has_real_help_entrypoint(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("one raw YouTube channel", completed.stdout)


if __name__ == "__main__":
    unittest.main()
