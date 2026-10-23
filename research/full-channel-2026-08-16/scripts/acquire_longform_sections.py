#!/usr/bin/env python3
"""Acquire one exact long-form public video as bounded, auditable sections."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit


SCHEMA = "longform-source-segments-v1"
MAX_SECTION_SECONDS = Decimal("600")
MAX_SEGMENT_BYTES = 800 * 1024 * 1024
MAX_FULL_SOURCE_BYTES = 4 * 1024 * 1024 * 1024
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".webm", ".mkv"}
DEMUXERS = {".mp4": "mov", ".webm": "matroska", ".mkv": "matroska"}
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 1800
POLICY = {
    "max_segment_bytes": MAX_SEGMENT_BYTES,
    "max_full_source_bytes": MAX_FULL_SOURCE_BYTES,
    "source_format_selector": "18/b[height<=360]",
    "section_youtube_player_client": "tv_embedded",
    "full_youtube_player_client": "default",
    "native_http_chunk_size": "256K",
    "playlist_allowed": False,
    "authentication_allowed": False,
    "forced_keyframe_reencode_allowed": False,
    "publication": "atomic-create-new-only",
    "input_protocol": "https-known-youtube-host-url-via-stdin",
    "probe_protocol_whitelist": "file,crypto,data",
}


def sanitize_error(value: str) -> str:
    redacted = re.sub(r"https?://\S+", "[URL]", value, flags=re.IGNORECASE)
    compact = " ".join(redacted.split())
    return compact[-1000:]


def _format_seconds(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001')):.6f}"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _canonical_json_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _reject_network_path(path: Path) -> None:
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    if normalized.startswith("\\\\") or normalized.lower().startswith("\\\\?\\unc\\"):
        raise ValueError("network or UNC paths are not allowed")
    if os.name == "nt":
        drive, _ = os.path.splitdrive(os.path.abspath(raw))
        if drive and int(ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")) == 4:
            raise ValueError("mapped network paths are not allowed")


def validate_local_path(path: Path, *, must_exist: bool) -> Path:
    _reject_network_path(path)
    absolute = Path(os.path.abspath(os.fspath(path)))
    _reject_network_path(absolute)
    if must_exist and not absolute.exists():
        raise FileNotFoundError(absolute)
    resolved = absolute.resolve(strict=must_exist)
    _reject_network_path(resolved)
    return resolved


def build_sections(
    total_duration: Decimal, max_section_seconds: Decimal = MAX_SECTION_SECONDS
) -> list[dict[str, object]]:
    if not total_duration.is_finite() or total_duration <= 0:
        raise ValueError("total duration must be positive and finite")
    if (
        not max_section_seconds.is_finite()
        or max_section_seconds <= 0
        or max_section_seconds > MAX_SECTION_SECONDS
    ):
        raise ValueError("section cap must be positive and no more than 600 seconds")
    rows: list[dict[str, object]] = []
    start = Decimal("0")
    index = 0
    while start < total_duration:
        end = min(start + max_section_seconds, total_duration)
        rows.append(
            {
                "index": index,
                "start_seconds": _format_seconds(start),
                "end_seconds": _format_seconds(end),
                "duration_seconds": _format_seconds(end - start),
                "directory": f"segments/segment-{index:05d}",
            }
        )
        start = end
        index += 1
    return rows


def _read_exact_url(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if len(lines) != 1 or not lines[0] or content not in {lines[0], lines[0] + "\n"}:
        raise ValueError("URL file must contain exactly one UTF-8 line")
    raw = lines[0]
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError("only an exact public HTTPS YouTube URL is allowed")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    return raw


def _youtube_video_id(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/")
    else:
        values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
        candidate = values[0] if len(values) == 1 else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", candidate):
        raise ValueError("exact YouTube URL has no single valid video ID")
    return candidate


def initialize_contract(
    url_file: Path,
    output: Path,
    identity: dict[str, object],
    *,
    max_section_seconds: Decimal = MAX_SECTION_SECONDS,
) -> dict[str, object]:
    url_path = validate_local_path(url_file, must_exist=True)
    output_path = validate_local_path(output, must_exist=False)
    if output_path.exists():
        raise FileExistsError("contract output already exists")
    url = _read_exact_url(url_path)
    required = {
        "video_id",
        "title",
        "uploader",
        "duration_seconds",
        "availability",
        "is_live",
    }
    if set(identity) != required:
        raise ValueError("source identity shape mismatch")
    try:
        duration = Decimal(str(identity["duration_seconds"]))
    except InvalidOperation:
        raise ValueError("invalid source duration") from None
    if identity["is_live"] is not False or identity["availability"] != "public":
        raise ValueError("source must be a public non-live exact item")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", str(identity["video_id"])):
        raise ValueError("invalid exact video ID")
    if _youtube_video_id(url) != str(identity["video_id"]):
        raise ValueError("exact URL video ID does not match inspected identity")
    sections = build_sections(duration, max_section_seconds)
    source_identity = {
        **identity,
        "duration_seconds": _format_seconds(duration),
    }
    source = {
        "url": url,
        "url_sha256": _sha256_bytes(url.encode("utf-8")),
        **source_identity,
        "identity_sha256": _canonical_json_hash(source_identity),
    }
    contract: dict[str, object] = {
        "schema": SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "pipeline_provenance": {
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "policy_sha256": _canonical_json_hash(POLICY),
            "python_version": ".".join(map(str, os.sys.version_info[:3])),
        },
        "source": source,
        "settings": {
            "max_section_seconds": _format_seconds(max_section_seconds),
            "max_segment_bytes": MAX_SEGMENT_BYTES,
            "max_full_source_bytes": MAX_FULL_SOURCE_BYTES,
            "source_format_selector": "18/b[height<=360]",
            "section_youtube_player_client": "tv_embedded",
            "full_youtube_player_client": "default",
            "native_http_chunk_size": "256K",
            "playlist_allowed": False,
            "authentication_allowed": False,
            "forced_keyframe_reencode_allowed": False,
            "publication": "atomic-create-new-only",
        },
        "sections": sections,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_path.name}.partial-", dir=output_path.parent)
    )
    committed = False
    try:
        (staging / "segments").mkdir()
        with (staging / "contract.json").open(
            "x", encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(contract, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.rename(staging, output_path)
        committed = True
        return contract
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _load_contract(contract_root: Path) -> tuple[Path, dict[str, object]]:
    root = validate_local_path(contract_root, must_exist=True)
    path = root / "contract.json"
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("contract JSON is unreadable") from None
    if not isinstance(contract, dict) or set(contract) != {
        "schema",
        "created_utc",
        "pipeline_provenance",
        "source",
        "settings",
        "sections",
    }:
        raise RuntimeError("contract schema mismatch")
    if contract.get("schema") != SCHEMA:
        raise RuntimeError("contract schema mismatch")
    provenance = contract.get("pipeline_provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "script_sha256",
        "policy_sha256",
        "python_version",
    }:
        raise RuntimeError("contract pipeline provenance mismatch")
    if provenance.get("script_sha256") != sha256_file(Path(__file__).resolve()):
        raise RuntimeError("contract extractor script hash no longer matches")
    if provenance.get("policy_sha256") != _canonical_json_hash(POLICY):
        raise RuntimeError("contract acquisition policy hash no longer matches")
    source = contract.get("source")
    settings = contract.get("settings")
    sections = contract.get("sections")
    if not isinstance(source, dict) or not isinstance(settings, dict) or not isinstance(sections, list):
        raise RuntimeError("contract schema mismatch")
    url = source.get("url")
    if not isinstance(url, str) or source.get("url_sha256") != _sha256_bytes(url.encode("utf-8")):
        raise RuntimeError("contract URL identity mismatch")
    identity = {
        key: source.get(key)
        for key in (
            "video_id",
            "title",
            "uploader",
            "duration_seconds",
            "availability",
            "is_live",
        )
    }
    if source.get("identity_sha256") != _canonical_json_hash(identity):
        raise RuntimeError("contract source identity mismatch")
    try:
        planned = build_sections(
            Decimal(str(source["duration_seconds"])),
            Decimal(str(settings["max_section_seconds"])),
        )
    except (KeyError, InvalidOperation, ValueError):
        raise RuntimeError("contract section settings are invalid") from None
    if sections != planned:
        raise RuntimeError("contract requested coverage has a gap or overlap")
    expected_root_files = {"contract.json"}
    actual_root_files = {
        path.name for path in root.iterdir() if path.is_file()
    }
    allowed_root_files = expected_root_files | {"whole-item-complete.json", "assembled-source.json"}
    if not actual_root_files.issubset(allowed_root_files) or "contract.json" not in actual_root_files:
        raise RuntimeError("contract root file set mismatch")
    if not (root / "segments").is_dir():
        raise RuntimeError("contract segments directory is missing")
    return root, contract


def _run_json(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError("ffprobe failed for acquired segment")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe returned invalid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeError("ffprobe returned invalid metadata")
    return value


def _rate(value: object) -> str:
    try:
        return f"{float(Fraction(str(value))):.9f}"
    except (ValueError, ZeroDivisionError):
        return "0.000000000"


def probe_media(path: Path) -> dict[str, object]:
    media = validate_local_path(path, must_exist=True)
    suffix = media.suffix.lower()
    if suffix not in ALLOWED_MEDIA_EXTENSIONS:
        raise RuntimeError("acquired segment media extension is unsupported")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is unavailable")
    raw = _run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,crypto,data",
            "-f",
            DEMUXERS[suffix],
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(media),
        ]
    )
    streams = raw.get("streams") if isinstance(raw.get("streams"), list) else []
    videos = [
        row
        for row in streams
        if isinstance(row, dict) and row.get("codec_type") == "video"
    ]
    if len(videos) != 1:
        raise RuntimeError("acquired segment must contain exactly one video stream")
    stream = videos[0]
    fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    try:
        duration = Decimal(str(fmt["duration"]))
        stream_start = Decimal(str(stream["start_time"]))
        format_start = Decimal(str(fmt["start_time"]))
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise RuntimeError("acquired segment ffprobe identity is incomplete") from None
    if duration <= 0:
        raise RuntimeError("acquired segment duration is invalid")
    if abs(stream_start) > Decimal("0.000001") or abs(format_start) > Decimal("0.000001"):
        raise RuntimeError("acquired segment timestamp origin is non-zero")
    if width <= 0 or height <= 0 or height > 720:
        raise RuntimeError("acquired segment exceeds the 720p source boundary")
    return {
        "format_name": str(fmt.get("format_name", "")),
        "duration_seconds": _format_seconds(duration),
        "format_start_seconds": _format_seconds(format_start),
        "video_stream": {
            "codec_name": str(stream.get("codec_name", "")),
            "profile": str(stream.get("profile", "")),
            "width": width,
            "height": height,
            "pix_fmt": str(stream.get("pix_fmt", "")),
            "avg_fps": _rate(stream.get("avg_frame_rate")),
            "nominal_fps": _rate(stream.get("r_frame_rate")),
            "start_seconds": _format_seconds(stream_start),
        },
    }


def _directory_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _yt_dlp_section_runner(
    url: str,
    section: dict[str, object],
    staging: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError("yt-dlp is unavailable")
    command = build_ytdlp_section_command(executable, section)
    _run_monitored_ytdlp(
        command,
        url,
        staging,
        max_bytes=MAX_SEGMENT_BYTES,
        timeout_seconds=timeout_seconds,
    )


def _run_monitored_ytdlp(
    command: list[str],
    url: str,
    staging: Path,
    *,
    max_bytes: int,
    timeout_seconds: int,
) -> None:
    process = subprocess.Popen(
        command,
        cwd=staging,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    counts = {"stdout": 0, "stderr": 0}
    overflow: list[str] = []
    stderr_tail = bytearray()
    lock = threading.Lock()

    def drain(stream, label: str, limit: int) -> None:
        while True:
            try:
                block = stream.read(65536)
            except OSError:
                return
            if not block:
                return
            with lock:
                counts[label] += len(block)
                if label == "stderr":
                    stderr_tail.extend(block)
                    if len(stderr_tail) > 8192:
                        del stderr_tail[:-8192]
                if counts[label] > limit:
                    overflow.append(label)
                    return

    threads = [
        threading.Thread(target=drain, args=(process.stdout, "stdout", MAX_STDOUT_BYTES), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr", MAX_STDERR_BYTES), daemon=True),
    ]
    for thread in threads:
        thread.start()
    failure: str | None = None
    try:
        process.stdin.write((url + "\n").encode("utf-8"))
        process.stdin.close()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if overflow:
                failure = f"{overflow[0]} status limit exceeded"
                _terminate(process)
                break
            if _directory_bytes(staging) > max_bytes:
                failure = f"staging exceeds the {max_bytes}-byte ceiling"
                _terminate(process)
                break
            if time.monotonic() >= deadline:
                failure = "segment acquisition timed out"
                _terminate(process)
                break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            _terminate(process)
        for thread in threads:
            thread.join(timeout=5)
    if failure:
        raise RuntimeError(failure)
    if process.returncode != 0:
        detail = sanitize_error(stderr_tail.decode("utf-8", errors="replace"))
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"yt-dlp section acquisition failed with exit {process.returncode}{suffix}"
        )


def build_ytdlp_section_command(
    executable: str, section: dict[str, object]
) -> list[str]:
    try:
        start = Decimal(str(section["start_seconds"]))
        end = Decimal(str(section["end_seconds"]))
    except (KeyError, InvalidOperation):
        raise ValueError("section boundaries are invalid") from None
    if start < 0 or end <= start or end - start > MAX_SECTION_SECONDS:
        raise ValueError("section boundaries are invalid or exceed 600 seconds")
    selector = f"*{_format_seconds(start)}-{_format_seconds(end)}"
    return [
        executable,
        "--ignore-config",
        "--no-remote-components",
        "--no-playlist",
        "--playlist-end",
        "1",
        "--extractor-args",
        "youtube:player_client=tv_embedded",
        "--no-overwrites",
        "--no-continue",
        "--no-part",
        "--no-progress",
        "--no-warnings",
        "--restrict-filenames",
        "--windows-filenames",
        "--download-sections",
        selector,
        "--format",
        "18/b[height<=360]",
        "--output",
        "media.%(ext)s",
        "--batch-file",
        "-",
    ]


def build_ytdlp_full_command(executable: str) -> list[str]:
    return [
        executable,
        "--ignore-config",
        "--no-remote-components",
        "--no-playlist",
        "--playlist-end",
        "1",
        "--no-overwrites",
        "--no-continue",
        "--no-part",
        "--no-progress",
        "--no-warnings",
        "--restrict-filenames",
        "--windows-filenames",
        "--format",
        "18/b[height<=360]",
        "--http-chunk-size",
        "256K",
        "--output",
        "media.%(ext)s",
        "--batch-file",
        "-",
    ]


def _yt_dlp_full_runner(
    url: str,
    _section: dict[str, object],
    staging: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError("yt-dlp is unavailable")
    _run_monitored_ytdlp(
        build_ytdlp_full_command(executable),
        url,
        staging,
        max_bytes=MAX_FULL_SOURCE_BYTES,
        timeout_seconds=timeout_seconds,
    )


def _section_for(contract: dict[str, object], index: int) -> dict[str, object]:
    sections = contract["sections"]
    if not isinstance(index, int) or index < 0 or not isinstance(sections, list) or index >= len(sections):
        raise IndexError("section index is out of range")
    section = sections[index]
    if not isinstance(section, dict) or section.get("index") != index:
        raise RuntimeError("contract section index mismatch")
    return section


def _validate_duration(probe: dict[str, object], section: dict[str, object]) -> None:
    actual = Decimal(str(probe["duration_seconds"]))
    expected = Decimal(str(section["duration_seconds"]))
    fps = Decimal(str(probe["video_stream"]["avg_fps"]))
    tolerance = max(Decimal("0.250000"), Decimal("2") / fps if fps > 0 else Decimal("0.250000"))
    if abs(actual - expected) > tolerance:
        raise RuntimeError(
            f"acquired segment duration differs from requested section by more than {tolerance} seconds"
        )


def _tool_version(executable: str) -> str:
    path = shutil.which(executable)
    if not path:
        return "unavailable"
    completed = subprocess.run(
        [path, "--version"], check=False, capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode != 0:
        return "unavailable"
    return completed.stdout.splitlines()[0].strip()[:200]


def _validate_staged_segment(staging: Path, manifest: dict[str, object]) -> None:
    media_name = str(manifest["media"]["file"])
    expected = {"manifest.json", media_name}
    actual = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise RuntimeError("segment staged file set mismatch")
    stored = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    if stored != manifest:
        raise RuntimeError("segment manifest publication mismatch")
    media = staging / media_name
    if sha256_file(media) != manifest["media"]["sha256"]:
        raise RuntimeError("segment staged hash mismatch")


def verify_full_source(contract_root: Path) -> dict[str, object]:
    root, contract = _load_contract(contract_root)
    directory = root / "full-source"
    if not directory.is_dir():
        raise RuntimeError("full source directory is missing")
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("full source manifest is unreadable") from None
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "created_utc",
        "source",
        "acquisition",
        "media",
    }:
        raise RuntimeError("full source manifest schema mismatch")
    if manifest.get("schema") != "longform-full-source-v1":
        raise RuntimeError("full source manifest schema mismatch")
    source = contract["source"]
    expected_source = {
        "url_sha256": source["url_sha256"],
        "identity_sha256": source["identity_sha256"],
        "video_id": source["video_id"],
    }
    if manifest.get("source") != expected_source:
        raise RuntimeError("full source identity mismatch")
    media_record = manifest.get("media")
    if not isinstance(media_record, dict) or set(media_record) != {"file", "bytes", "sha256", "ffprobe"}:
        raise RuntimeError("full source media record schema mismatch")
    name = media_record.get("file")
    if not isinstance(name, str) or Path(name).name != name:
        raise RuntimeError("full source media path is invalid")
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    if actual_files != {"manifest.json", name}:
        raise RuntimeError("full source file set mismatch")
    media = directory / name
    if (
        not media.is_file()
        or media.stat().st_size != media_record.get("bytes")
        or media.stat().st_size > MAX_FULL_SOURCE_BYTES
        or sha256_file(media) != media_record.get("sha256")
    ):
        raise RuntimeError("full source hash, size or byte ceiling mismatch")
    current_probe = probe_media(media)
    if current_probe != media_record.get("ffprobe"):
        raise RuntimeError("full source ffprobe identity mismatch")
    actual_duration = Decimal(str(current_probe["duration_seconds"]))
    expected_duration = Decimal(str(source["duration_seconds"]))
    if abs(actual_duration - expected_duration) > Decimal("0.250000"):
        raise RuntimeError("full source duration differs from inspected item")
    return manifest


def acquire_full_source(
    contract_root: Path,
    *,
    runner: Callable[[str, dict[str, object], Path], None] | None = None,
) -> dict[str, object]:
    root, contract = _load_contract(contract_root)
    final = root / "full-source"
    if final.exists():
        try:
            return verify_full_source(root)
        except Exception as exc:
            raise RuntimeError(f"existing full source is invalid: {exc}") from None
    staging = Path(
        tempfile.mkdtemp(prefix=".full-source.partial-", dir=root)
    )
    committed = False
    try:
        selected = runner or _yt_dlp_full_runner
        full_section = {
            "start_seconds": "0.000000",
            "end_seconds": contract["source"]["duration_seconds"],
            "duration_seconds": contract["source"]["duration_seconds"],
        }
        selected(str(contract["source"]["url"]), full_section, staging)
        media_files = [
            path
            for path in staging.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_MEDIA_EXTENSIONS
        ]
        if len(media_files) != 1 or len(list(staging.iterdir())) != 1:
            raise RuntimeError("full source acquisition must yield exactly one media file")
        media = media_files[0]
        if media.stat().st_size <= 0 or media.stat().st_size > MAX_FULL_SOURCE_BYTES:
            raise RuntimeError("full source exceeds the bounded byte policy")
        probe = probe_media(media)
        actual_duration = Decimal(str(probe["duration_seconds"]))
        expected_duration = Decimal(str(contract["source"]["duration_seconds"]))
        if abs(actual_duration - expected_duration) > Decimal("0.250000"):
            raise RuntimeError("full source duration differs from inspected item")
        source = contract["source"]
        manifest: dict[str, object] = {
            "schema": "longform-full-source-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "url_sha256": source["url_sha256"],
                "identity_sha256": source["identity_sha256"],
                "video_id": source["video_id"],
            },
            "acquisition": {
                "runner": "injected-test-runner" if runner is not None else "yt-dlp-native-full-v1",
                "yt_dlp_version": _tool_version("yt-dlp"),
                "ffprobe_version": _tool_version("ffprobe"),
                "format_selector": POLICY["source_format_selector"],
                "player_client": POLICY["full_youtube_player_client"],
                "playlist_allowed": False,
                "authentication_allowed": False,
                "atomic_no_clobber": True,
            },
            "media": {
                "file": media.name,
                "bytes": media.stat().st_size,
                "sha256": sha256_file(media),
                "ffprobe": probe,
            },
        }
        with (staging / "manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        _validate_staged_segment(staging, manifest)
        os.rename(staging, final)
        committed = True
        return manifest
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _local_stream_copy_runner(
    full_source: Path,
    _url: str,
    section: dict[str, object],
    staging: Path,
) -> None:
    suffix = full_source.suffix.lower()
    if suffix not in ALLOWED_MEDIA_EXTENSIONS:
        raise RuntimeError("full source media extension is unsupported")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")
    output = staging / f"media{suffix}"
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        str(section["start_seconds"]),
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        DEMUXERS[suffix],
        "-i",
        str(full_source),
        "-t",
        str(section["duration_seconds"]),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-reset_timestamps",
        "1",
        "-n",
        str(output),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"local stream-copy section failed: {sanitize_error(completed.stderr)}")


def verify_segment(contract_root: Path, index: int) -> dict[str, object]:
    root, contract = _load_contract(contract_root)
    section = _section_for(contract, index)
    segment = root / Path(str(section["directory"]))
    if not segment.is_dir():
        raise RuntimeError("segment directory is missing")
    manifest_path = segment / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("segment manifest is unreadable") from None
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "created_utc",
        "source",
        "section",
        "acquisition",
        "media",
    }:
        raise RuntimeError("segment manifest schema mismatch")
    if manifest.get("schema") != "longform-source-segment-v1":
        raise RuntimeError("segment manifest schema mismatch")
    source = contract["source"]
    expected_source = {
        "url_sha256": source["url_sha256"],
        "identity_sha256": source["identity_sha256"],
        "video_id": source["video_id"],
    }
    acquisition = manifest.get("acquisition")
    if (
        isinstance(acquisition, dict)
        and acquisition.get("runner") == "ffmpeg-local-stream-copy-v1"
    ):
        full_manifest = verify_full_source(root)
        expected_source["full_source_sha256"] = full_manifest["media"]["sha256"]
    if manifest.get("source") != expected_source or manifest.get("section") != section:
        raise RuntimeError("segment source or section identity mismatch")
    media_record = manifest.get("media")
    if not isinstance(media_record, dict) or set(media_record) != {"file", "bytes", "sha256", "ffprobe"}:
        raise RuntimeError("segment media record schema mismatch")
    media_name = media_record.get("file")
    if not isinstance(media_name, str) or Path(media_name).name != media_name:
        raise RuntimeError("segment media path is invalid")
    expected_files = {"manifest.json", media_name}
    actual_files = {
        path.relative_to(segment).as_posix()
        for path in segment.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError("segment file set mismatch")
    media = segment / media_name
    if (
        not media.is_file()
        or media.stat().st_size != media_record.get("bytes")
        or sha256_file(media) != media_record.get("sha256")
    ):
        raise RuntimeError("segment media hash or size mismatch")
    current_probe = probe_media(media)
    if current_probe != media_record.get("ffprobe"):
        raise RuntimeError("segment ffprobe identity mismatch")
    _validate_duration(current_probe, section)
    return manifest


def acquire_section(
    contract_root: Path,
    index: int,
    *,
    runner: Callable[[str, dict[str, object], Path], None] | None = None,
) -> dict[str, object]:
    root, contract = _load_contract(contract_root)
    section = _section_for(contract, index)
    final = root / Path(str(section["directory"]))
    if final.exists():
        try:
            return verify_segment(root, index)
        except Exception as exc:
            raise RuntimeError(f"existing segment is invalid: {exc}") from None
    segments_root = root / "segments"
    staging = Path(
        tempfile.mkdtemp(prefix=f".segment-{index:05d}.partial-", dir=segments_root)
    )
    committed = False
    try:
        full_manifest: dict[str, object] | None = None
        if runner is not None:
            selected_runner = runner
            runner_name = "injected-test-runner"
        elif (root / "full-source").exists():
            full_manifest = verify_full_source(root)
            full_media = root / "full-source" / str(full_manifest["media"]["file"])

            def selected_runner(url, selected_section, selected_staging):
                _local_stream_copy_runner(
                    full_media, url, selected_section, selected_staging
                )

            runner_name = "ffmpeg-local-stream-copy-v1"
        else:
            selected_runner = _yt_dlp_section_runner
            runner_name = "yt-dlp-section-v1"
        selected_runner(str(contract["source"]["url"]), section, staging)
        media_files = [
            path
            for path in staging.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_MEDIA_EXTENSIONS
        ]
        if len(media_files) != 1 or len(list(staging.iterdir())) != 1:
            raise RuntimeError("section acquisition must yield exactly one media file")
        media = media_files[0]
        if media.stat().st_size <= 0 or media.stat().st_size > MAX_SEGMENT_BYTES:
            raise RuntimeError("acquired segment exceeds the bounded byte policy")
        probe = probe_media(media)
        _validate_duration(probe, section)
        source = contract["source"]
        manifest_source = {
            "url_sha256": source["url_sha256"],
            "identity_sha256": source["identity_sha256"],
            "video_id": source["video_id"],
        }
        if full_manifest is not None:
            manifest_source["full_source_sha256"] = full_manifest["media"]["sha256"]
        manifest: dict[str, object] = {
            "schema": "longform-source-segment-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source": manifest_source,
            "section": section,
            "acquisition": {
                "runner": runner_name,
                "yt_dlp_version": _tool_version("yt-dlp"),
                "ffprobe_version": _tool_version("ffprobe"),
                "playlist_allowed": False,
                "authentication_allowed": False,
                "forced_keyframe_reencode_allowed": False,
                "atomic_no_clobber": True,
            },
            "media": {
                "file": media.name,
                "bytes": media.stat().st_size,
                "sha256": sha256_file(media),
                "ffprobe": probe,
            },
        }
        with (staging / "manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        _validate_staged_segment(staging, manifest)
        os.rename(staging, final)
        committed = True
        return manifest
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _segments_tree(rows: list[dict[str, object]]) -> str:
    material = "\n".join(
        "\0".join(
            [
                str(row["index"]),
                str(row["start_seconds"]),
                str(row["end_seconds"]),
                str(row["manifest_sha256"]),
                str(row["media_sha256"]),
            ]
        )
        for row in rows
    ).encode("utf-8")
    return _sha256_bytes(material)


def verify_contract(
    contract_root: Path, *, publish_complete: bool = False
) -> dict[str, object]:
    root, contract = _load_contract(contract_root)
    sections = contract["sections"]
    assert isinstance(sections, list)
    expected_names = {Path(str(row["directory"])).name for row in sections}
    actual_names = {
        path.name for path in (root / "segments").iterdir() if path.is_dir()
    }
    missing = sorted(expected_names - actual_names)
    if missing:
        raise RuntimeError(f"missing planned segment: {missing[0]}")
    if actual_names != expected_names:
        raise RuntimeError("segment directory set has extra or unexpected entries")
    if any(path.is_file() for path in (root / "segments").iterdir()):
        raise RuntimeError("segment directory set contains unexpected files")
    rows: list[dict[str, object]] = []
    total_bytes = 0
    previous_end = Decimal("0")
    gap = Decimal("0")
    overlap = Decimal("0")
    for index, section in enumerate(sections):
        manifest = verify_segment(root, index)
        start = Decimal(str(section["start_seconds"]))
        end = Decimal(str(section["end_seconds"]))
        if start > previous_end:
            gap += start - previous_end
        elif start < previous_end:
            overlap += previous_end - start
        previous_end = end
        media = manifest["media"]
        segment_dir = root / Path(str(section["directory"]))
        manifest_hash = sha256_file(segment_dir / "manifest.json")
        byte_count = int(media["bytes"])
        total_bytes += byte_count
        rows.append(
            {
                "index": index,
                "start_seconds": section["start_seconds"],
                "end_seconds": section["end_seconds"],
                "manifest_sha256": manifest_hash,
                "media_file": media["file"],
                "media_bytes": byte_count,
                "media_sha256": media["sha256"],
            }
        )
    if gap != 0 or overlap != 0:
        raise RuntimeError("verified requested coverage has a gap or overlap")
    source = contract["source"]
    immutable: dict[str, object] = {
        "schema": "longform-source-complete-v1",
        "contract_schema": contract["schema"],
        "contract_sha256": sha256_file(root / "contract.json"),
        "source_identity_sha256": source["identity_sha256"],
        "video_id": source["video_id"],
        "segment_count": len(rows),
        "total_media_bytes": total_bytes,
        "coverage": {
            "start_seconds": "0.000000",
            "end_seconds": source["duration_seconds"],
            "gap_seconds": _format_seconds(gap),
            "overlap_seconds": _format_seconds(overlap),
        },
        "segments_tree_sha256": _segments_tree(rows),
        "segments": rows,
    }
    complete_path = root / "whole-item-complete.json"
    if complete_path.exists():
        try:
            stored = json.loads(complete_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RuntimeError("existing whole-item completion gate is unreadable") from None
        if not isinstance(stored, dict) or set(stored) != set(immutable) | {"completed_utc"}:
            raise RuntimeError("existing whole-item completion gate schema mismatch")
        if {key: value for key, value in stored.items() if key != "completed_utc"} != immutable:
            raise RuntimeError("existing whole-item completion gate does not match verified segments")
        return stored
    result = {
        **immutable,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
    }
    if publish_complete:
        temporary = root / f".whole-item-complete.{os.getpid()}.partial.json"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.rename(temporary, complete_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return result


def _read_identity_file(path: Path) -> dict[str, object]:
    identity_path = validate_local_path(path, must_exist=True)
    try:
        value = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("identity JSON is unreadable") from None
    if not isinstance(value, dict):
        raise ValueError("identity JSON must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--url-file", required=True)
    init_parser.add_argument("--identity-file", required=True)
    init_parser.add_argument("--output-dir", required=True)
    init_parser.add_argument("--max-section-seconds", default="600")

    acquire_parser = subparsers.add_parser("acquire")
    acquire_parser.add_argument("--contract-dir", required=True)
    acquire_parser.add_argument("--index", required=True, type=int)

    full_parser = subparsers.add_parser("acquire-full")
    full_parser.add_argument("--contract-dir", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--contract-dir", required=True)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--contract-dir", required=True)

    args = parser.parse_args(argv)
    if args.action == "init":
        try:
            cap = Decimal(args.max_section_seconds)
        except InvalidOperation:
            raise ValueError("max section seconds is invalid") from None
        result = initialize_contract(
            Path(args.url_file),
            Path(args.output_dir),
            _read_identity_file(Path(args.identity_file)),
            max_section_seconds=cap,
        )
    elif args.action == "acquire":
        result = acquire_section(Path(args.contract_dir), args.index)
    elif args.action == "acquire-full":
        result = acquire_full_source(Path(args.contract_dir))
    elif args.action == "verify":
        result = verify_contract(Path(args.contract_dir), publish_complete=False)
    else:
        result = verify_contract(Path(args.contract_dir), publish_complete=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
