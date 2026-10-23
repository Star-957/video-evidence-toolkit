from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@[^\s,@/\\]{3,30}$")
APPROVAL_SCOPE = "one_channel_full_uploads_raw_api_receipts"
DEFAULT_APPROVAL_MANIFEST = (
    Path(__file__).resolve().parents[1] / "youtube-collection-approval.json"
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed so the API-key header is never forwarded to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class YouTubeDataAPI:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://www.googleapis.com/youtube/v3",
        timeout_seconds: int = 30,
    ) -> None:
        if not api_key:
            raise ValueError("A YouTube Data API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"timeout_seconds={self._timeout_seconds!r})"
        )

    def get(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"{self._base_url}/{resource}?"
            + urllib.parse.urlencode(params, doseq=True)
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "neuro-raw-channel-receipt/2",
                "X-Goog-Api-Key": self._api_key,
            },
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(
                request, timeout=self._timeout_seconds
            ) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"YouTube Data API request failed with HTTP {exc.code}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError("YouTube Data API request failed") from None


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validate_channel_ref(channel_ref: str) -> str:
    if CHANNEL_ID_RE.fullmatch(channel_ref) or HANDLE_RE.fullmatch(channel_ref):
        return channel_ref
    raise ValueError(
        "channel_ref must be a single canonical UC channel ID or one @handle"
    )


def _validate_approval_manifest(path: Path, expected_reference: str) -> None:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("YouTube collection approval manifest is unavailable") from None
    if manifest.get("approved") is not True:
        raise ValueError("YouTube full-channel collection is not approved")
    if manifest.get("schema_version") != 1:
        raise ValueError("YouTube collection approval manifest schema is invalid")
    issuer = manifest.get("issuer")
    reference = manifest.get("reference")
    approved_at = manifest.get("approved_at")
    document_hash = manifest.get("approval_document_sha256")
    scope = manifest.get("scope")
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError("YouTube collection approval issuer is missing")
    if reference != expected_reference:
        raise ValueError("YouTube collection approval reference does not match")
    if not isinstance(scope, list) or APPROVAL_SCOPE not in scope:
        raise ValueError("YouTube collection approval scope is insufficient")
    if not isinstance(approved_at, str) or not approved_at.endswith("Z"):
        raise ValueError("YouTube collection approval timestamp is invalid")
    if not isinstance(document_hash, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", document_hash
    ):
        raise ValueError("YouTube collection approval document hash is invalid")


def run_raw_channel(
    api: Any,
    channel_ref: str,
    output_dir: Path,
    *,
    now: datetime,
    approval_reference: str,
) -> dict[str, Any]:
    """Store one channel's raw API responses without aggregates or metrics."""
    channel_ref = _validate_channel_ref(channel_ref)
    if not approval_reference.strip():
        raise ValueError("A documented collection approval reference is required")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Output already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.tmp-", dir=str(output_dir.parent)
        )
    )
    try:
        channel_params = {
            "part": "id,snippet,contentDetails,statistics",
            ("id" if channel_ref.startswith("UC") else "forHandle"): channel_ref,
        }
        channel_response = api.get("channels", channel_params)
        channels = channel_response.get("items", [])
        if len(channels) != 1:
            raise ValueError(
                f"Expected one channel for {channel_ref!r}, got {len(channels)}"
            )
        uploads_id = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        _write_json(temporary / "channel-response.json", channel_response)

        video_ids: list[str] = []
        page_token: str | None = None
        page_number = 0
        while True:
            page_number += 1
            params = {
                "part": "contentDetails,snippet,status",
                "playlistId": uploads_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            page = api.get("playlistItems", params)
            _write_json(
                temporary / f"playlist-page-{page_number:04d}.json", page
            )
            for item in page.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id:
                    video_ids.append(video_id)
            page_token = page.get("nextPageToken")
            if not page_token:
                break

        batch_number = 0
        for start in range(0, len(video_ids), 50):
            batch_number += 1
            response = api.get(
                "videos",
                {
                    "part": (
                        "snippet,contentDetails,status,statistics,"
                        "liveStreamingDetails"
                    ),
                    "id": ",".join(video_ids[start : start + 50]),
                    "maxResults": 50,
                },
            )
            _write_json(
                temporary / f"video-batch-{batch_number:04d}.json", response
            )

        receipt = {
            "schema_version": 3,
            "requested_ref": channel_ref,
            "retrieved_at": _utc_text(now),
            "delete_or_refresh_due_at": _utc_text(now + timedelta(days=29)),
            "storage_class": "approval_gated_raw_api_data",
            "policy_mode": "one_channel_raw_only_no_derived_data",
            "api_service": "YouTube Data API v3",
            "approval_reference": approval_reference,
        }
        _write_json(temporary / "receipt.json", receipt)
        # Windows target: os.rename is an atomic move and fails if destination
        # exists. Unlike os.replace, it does not request destination replacement.
        os.rename(temporary, output_dir)
        return receipt
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def run_cli(
    argv: list[str],
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    now: datetime | None = None,
    api_factory: Any = YouTubeDataAPI,
    approval_manifest_path: Path = DEFAULT_APPROVAL_MANIFEST,
) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one raw YouTube channel receipt without cross-channel "
            "aggregation or derived metrics."
        )
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--channel",
        required=True,
        action="append",
        help="One canonical channel ID or one @handle; may be supplied once",
    )
    args = parser.parse_args(argv)
    if len(args.channel) != 1:
        parser.error("--channel must be supplied exactly once")
    channel_ref = _validate_channel_ref(args.channel[0])
    environment = os.environ if environ is None else environ
    api_key = environment.get("YOUTUBE_DATA_API_KEY", "")
    if not api_key:
        raise ValueError("YOUTUBE_DATA_API_KEY is not set")
    approval_reference = environment.get(
        "YOUTUBE_DATA_API_COLLECTION_APPROVAL_REF", ""
    ).strip()
    if not approval_reference:
        raise ValueError(
            "YOUTUBE_DATA_API_COLLECTION_APPROVAL_REF is not set"
        )
    _validate_approval_manifest(approval_manifest_path, approval_reference)
    timestamp = datetime.now(timezone.utc) if now is None else now
    return run_raw_channel(
        api_factory(api_key),
        channel_ref,
        Path(args.output_dir),
        now=timestamp,
        approval_reference=approval_reference,
    )


if __name__ == "__main__":
    run_cli(os.sys.argv[1:])
