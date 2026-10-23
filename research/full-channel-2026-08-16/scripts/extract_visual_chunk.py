from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any


PIPELINE_VERSION = "visual-chunk-v5"
ALLOWED_MEDIA_EXTENSIONS = {
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}
DEMUXER_BY_EXTENSION = {
    ".avi": "avi",
    ".flv": "flv",
    ".m2ts": "mpegts",
    ".m4v": "mov",
    ".mkv": "matroska",
    ".mov": "mov",
    ".mp4": "mov",
    ".mpeg": "mpeg",
    ".mpg": "mpeg",
    ".mts": "mpegts",
    ".ts": "mpegts",
    ".webm": "matroska",
    ".wmv": "asf",
}
SHOWINFO_RE = re.compile(
    r"n:\s*(?P<n>\d+).*?pts_time:(?P<pts>[+\-0-9.eE]+).*?"
    r"s:(?P<width>\d+)x(?P<height>\d+).*?checksum:(?P<checksum>[0-9A-F]+)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise RuntimeError("ffprobe failed for the local source")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe returned invalid JSON") from None


def _get_drive_type(root: str) -> int:
    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _reject_network_path(path: Path) -> None:
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    if normalized.startswith("\\\\") or normalized.lower().startswith("\\\\?\\unc\\"):
        raise ValueError("network or UNC paths are not allowed")
    if os.name == "nt":
        drive, _ = os.path.splitdrive(os.path.abspath(raw))
        if drive:
            drive_type = _get_drive_type(f"{drive}\\")
            if drive_type == 4:
                raise ValueError("Mapped network paths are not allowed")


def _validate_local_source_reference(source: Path) -> Path:
    _reject_network_path(source)
    raw = os.fspath(source)
    absolute = Path(os.path.abspath(raw))
    if absolute.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
        raise ValueError("Source must be one allowed single media-file format")
    resolved = absolute.resolve()
    _reject_network_path(resolved)
    return resolved


def _validate_local_output_reference(output_dir: Path) -> Path:
    _reject_network_path(output_dir)
    absolute = Path(os.path.abspath(os.fspath(output_dir)))
    _reject_network_path(absolute)
    resolved = absolute.resolve()
    _reject_network_path(resolved)
    return resolved


def _frames_tree_hash(frames: list[dict[str, Any]]) -> str:
    material = "\n".join(
        f"{frame['relative_path']}\0{frame['sha256']}\0{frame['source_pts_seconds']:.9f}"
        for frame in frames
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def _validate_staged_manifest(
    temporary: Path, expected_manifest: dict[str, Any]
) -> None:
    try:
        stored = json.loads(
            (temporary / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("Manifest schema/hash validation failed") from None
    required_top = {
        "schema_version",
        "pipeline_version",
        "pipeline_provenance",
        "source",
        "settings",
        "frames_tree_sha256",
        "burst_summary",
        "frames",
    }
    if set(stored) != required_top or stored != expected_manifest:
        raise RuntimeError("Manifest schema/hash validation failed")
    if stored.get("schema_version") != 1 or stored.get("pipeline_version") != PIPELINE_VERSION:
        raise RuntimeError("Manifest schema/hash validation failed")
    frames = stored.get("frames")
    if not isinstance(frames, list) or not frames:
        raise RuntimeError("Manifest schema/hash validation failed")
    expected_files = {"manifest.json"}
    for frame in frames:
        relative_text = frame.get("relative_path")
        if isinstance(relative_text, str):
            expected_files.add(Path(relative_text).as_posix())
    actual_files = {
        candidate.relative_to(temporary).as_posix()
        for candidate in temporary.rglob("*")
        if candidate.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError("Manifest schema/hash validation failed")
    temporary_resolved = temporary.resolve()
    for index, frame in enumerate(frames):
        if frame.get("sequence") != index:
            raise RuntimeError("Manifest schema/hash validation failed")
        relative_text = frame.get("relative_path")
        if not isinstance(relative_text, str):
            raise RuntimeError("Manifest schema/hash validation failed")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Manifest schema/hash validation failed")
        candidate = (temporary / relative).resolve()
        try:
            if os.path.commonpath([temporary_resolved, candidate]) != str(
                temporary_resolved
            ):
                raise RuntimeError("Manifest schema/hash validation failed")
        except ValueError:
            raise RuntimeError("Manifest schema/hash validation failed") from None
        expected_hash = frame.get("sha256", "")
        if (
            not candidate.is_file()
            or not re.fullmatch(r"[0-9A-F]{64}", expected_hash)
            or _sha256(candidate) != expected_hash
        ):
            raise RuntimeError("Manifest schema/hash validation failed")
    if _frames_tree_hash(frames) != stored.get("frames_tree_sha256"):
        raise RuntimeError("Manifest schema/hash validation failed")


def _rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _probe(source: Path, demuxer: str) -> dict[str, Any]:
    payload = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-protocol_whitelist",
            "file,pipe",
            "-of",
            "json",
            "-f",
            demuxer,
            str(source),
        ]
    )
    video_stream = next(
        (
            stream
            for stream in payload.get("streams", [])
            if stream.get("codec_type") == "video"
            and stream.get("disposition", {}).get("attached_pic", 0) != 1
        ),
        None,
    )
    if video_stream is None:
        raise ValueError("Local source has no video stream")
    duration_text = video_stream.get("duration") or payload.get("format", {}).get("duration")
    duration = float(duration_text) if duration_text is not None else None
    stream_start_text = video_stream.get("start_time")
    format_start_text = payload.get("format", {}).get("start_time")
    stream_start = (
        float(stream_start_text) if stream_start_text is not None else None
    )
    format_start = (
        float(format_start_text) if format_start_text is not None else None
    )
    timestamp_origins = [
        value for value in (stream_start, format_start) if value is not None
    ]
    if not timestamp_origins:
        raise RuntimeError("Timestamp origin is unavailable")
    if any(abs(value) > 0.000001 for value in timestamp_origins):
        raise RuntimeError("Non-zero timestamp origin is unsupported")
    avg_fps = _rate(video_stream.get("avg_frame_rate"))
    nominal_fps = _rate(video_stream.get("r_frame_rate"))
    return {
        "duration_seconds": duration,
        "stream_start_time_seconds": stream_start,
        "format_start_time_seconds": format_start,
        "timestamp_origin_policy": "require_reported_origin_within_1us_of_zero",
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec_name": video_stream.get("codec_name"),
        "pixel_format": video_stream.get("pix_fmt"),
        "avg_frame_rate": video_stream.get("avg_frame_rate"),
        "r_frame_rate": video_stream.get("r_frame_rate"),
        "avg_fps": avg_fps,
        "nominal_fps": nominal_fps,
        "time_base": video_stream.get("time_base"),
        "color_range": video_stream.get("color_range"),
        "color_space": video_stream.get("color_space"),
        "color_transfer": video_stream.get("color_transfer"),
        "color_primaries": video_stream.get("color_primaries"),
        "stream_index": video_stream.get("index"),
        "forced_input_demuxer": demuxer,
        "probed_format_name": payload.get("format", {}).get("format_name"),
    }


def _ffmpeg_version() -> str:
    completed = subprocess.run(
        ["ffmpeg", "-version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("FFmpeg version could not be recorded")
    return completed.stdout.splitlines()[0]


def _validate_request(
    *,
    mode: str,
    start_seconds: float,
    duration_seconds: float,
    baseline_fps: int | None,
    max_native_fps: int | None,
    burst_trigger: str | None,
    long_edge: int,
    max_frames: int,
) -> None:
    if start_seconds < 0:
        raise ValueError("start_seconds must be non-negative")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if long_edge < 64 or long_edge > 4096:
        raise ValueError("long_edge must be between 64 and 4096")
    if max_frames < 1:
        raise ValueError("max_frames must be positive")
    if mode == "baseline":
        if baseline_fps not in {3, 4, 5}:
            raise ValueError("baseline_fps must be 3, 4, or 5")
        if max_native_fps is not None:
            raise ValueError("max_native_fps is only valid for native-burst")
        if burst_trigger is not None:
            raise ValueError("burst_trigger is only valid for native-burst")
        if duration_seconds > 600:
            raise ValueError("baseline chunks are capped at 600 seconds")
    elif mode == "native-burst":
        if baseline_fps is not None:
            raise ValueError("baseline_fps is only valid for baseline mode")
        if max_native_fps not in {60, 120}:
            raise ValueError("max_native_fps must be 60 or 120")
        if not isinstance(burst_trigger, str) or not burst_trigger.strip():
            raise ValueError("native-burst requires a non-empty burst_trigger")
        if duration_seconds > 5:
            raise ValueError("native bursts are capped at 5 seconds")
    else:
        raise ValueError("mode must be baseline or native-burst")


def _scale_filter(long_edge: int) -> str:
    return (
        f"scale=w='min({long_edge},iw)':h='min({long_edge},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
    )


def run_visual_chunk(
    source: Path,
    output_dir: Path,
    *,
    mode: str,
    start_seconds: float,
    duration_seconds: float,
    baseline_fps: int | None,
    max_native_fps: int | None,
    burst_trigger: str | None,
    long_edge: int,
    max_frames: int,
) -> dict[str, Any]:
    """Extract a bounded visual chunk from one authorised local video file."""
    source = _validate_local_source_reference(Path(source))
    output_dir = _validate_local_output_reference(Path(output_dir))
    _validate_request(
        mode=mode,
        start_seconds=start_seconds,
        duration_seconds=duration_seconds,
        baseline_fps=baseline_fps,
        max_native_fps=max_native_fps,
        burst_trigger=burst_trigger,
        long_edge=long_edge,
        max_frames=max_frames,
    )
    if not source.is_file():
        raise FileNotFoundError(f"Local source does not exist: {source}")
    if output_dir.exists():
        raise FileExistsError(f"Output already exists: {output_dir}")

    source_hash_before = _sha256(source)
    input_demuxer = DEMUXER_BY_EXTENSION[source.suffix.lower()]
    probe = _probe(source, input_demuxer)
    source_duration = probe["duration_seconds"]
    if source_duration is not None and start_seconds + duration_seconds > source_duration + 0.05:
        raise ValueError("Requested range exceeds source duration")
    observed_rate = probe["avg_fps"] or probe["nominal_fps"]
    if mode == "native-burst":
        if observed_rate <= 0:
            raise ValueError("Source native FPS is unknown")
        if observed_rate > max_native_fps + 0.01:
            raise ValueError("Source native FPS exceeds declared burst ceiling")
        predicted = math.ceil(duration_seconds * observed_rate - 1e-9)
    else:
        if observed_rate <= 0:
            raise ValueError("Source FPS is unknown")
        if observed_rate + 0.001 < int(baseline_fps):
            raise ValueError("Source FPS is below requested baseline FPS")
        predicted = math.ceil(duration_seconds * int(baseline_fps) - 1e-9)
    if predicted > max_frames:
        raise ValueError("Predicted frame count exceeds max_frames")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent)
    )
    frames_dir = temporary / "frames"
    frames_dir.mkdir()
    try:
        filters = []
        if mode == "baseline":
            filters.append(
                "select='"
                f"lt(t,{duration_seconds:.9f})*gte(t,selected_n/{baseline_fps})"
                "'"
            )
        else:
            filters.append(f"select='lt(t,{duration_seconds:.9f})'")
        filters.extend([_scale_filter(long_edge), "showinfo"])
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-ss",
            format(start_seconds, ".9f"),
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            input_demuxer,
            "-i",
            str(source),
            "-t",
            format(duration_seconds, ".9f"),
            "-map",
            f"0:{probe['stream_index']}",
            "-an",
            "-vf",
            ",".join(filters),
            "-fps_mode",
            "passthrough",
            "-threads",
            "1",
            "-start_number",
            "0",
            "-frames:v",
            str(max_frames + 1),
            "-q:v",
            "3",
            "-pix_fmt",
            "yuvj420p",
            str(frames_dir / "frame_%08d.jpg"),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError("FFmpeg visual extraction failed")

        showinfo = {}
        for line in completed.stderr.splitlines():
            match = SHOWINFO_RE.search(line)
            if match:
                showinfo[int(match.group("n"))] = {
                    "relative_pts_seconds": float(match.group("pts")),
                    "width": int(match.group("width")),
                    "height": int(match.group("height")),
                    "decoded_checksum": match.group("checksum"),
                }
        images = sorted(frames_dir.glob("frame_*.jpg"))
        if not images:
            raise RuntimeError("FFmpeg produced no visual frames")
        if len(images) > max_frames:
            raise RuntimeError("Extracted frame count exceeded max_frames")
        if len(showinfo) != len(images):
            raise RuntimeError("Frame/PTS evidence count mismatch")

        frames = []
        previous_pts = None
        for index, image_path in enumerate(images):
            evidence = showinfo.get(index)
            if evidence is None:
                raise RuntimeError("Missing PTS evidence for extracted frame")
            source_pts = start_seconds + evidence["relative_pts_seconds"]
            if previous_pts is not None and source_pts <= previous_pts:
                raise RuntimeError("Extracted frame PTS is not strictly monotonic")
            previous_pts = source_pts
            sample_target = (
                start_seconds + (index / int(baseline_fps))
                if mode == "baseline"
                else None
            )
            frames.append(
                {
                    "sequence": index,
                    "relative_path": f"frames/{image_path.name}",
                    "source_pts_seconds": round(source_pts, 9),
                    "sample_target_seconds": (
                        round(sample_target, 9) if sample_target is not None else None
                    ),
                    "sampling_error_seconds": (
                        round(source_pts - sample_target, 9)
                        if sample_target is not None
                        else None
                    ),
                    "relative_pts_seconds": round(
                        evidence["relative_pts_seconds"], 9
                    ),
                    "width": evidence["width"],
                    "height": evidence["height"],
                    "processed_frame_checksum": evidence["decoded_checksum"],
                    "sha256": _sha256(image_path),
                }
            )

        if mode == "baseline":
            expected_count = math.ceil(
                duration_seconds * int(baseline_fps) - 1e-9
            )
            max_lag = max((1.25 / observed_rate), 0.001)
            errors = [frame["sampling_error_seconds"] for frame in frames]
            if len(frames) != expected_count or any(
                error < -1e-6 or error > max_lag for error in errors
            ):
                raise RuntimeError("Baseline target coverage gate failed")
            burst_summary = None
        else:
            relative_pts = [frame["relative_pts_seconds"] for frame in frames]
            distinct_pts = len(set(relative_pts))
            duplicate_pts = len(relative_pts) - distinct_pts
            window_seconds = min(1.0, duration_seconds)
            max_window_count = 0
            left = 0
            for right, pts in enumerate(relative_pts):
                while pts - relative_pts[left] >= window_seconds:
                    left += 1
                max_window_count = max(max_window_count, right - left + 1)
            allowed_window_count = math.ceil(
                window_seconds * int(max_native_fps) - 1e-9
            )
            if duplicate_pts or max_window_count > allowed_window_count:
                raise RuntimeError("Native burst PTS density exceeds declared ceiling")
            burst_summary = {
                "trigger": burst_trigger.strip(),
                "requested_rate_mode": f"source_native_up_to_{max_native_fps}_fps",
                "actual_first_source_pts_seconds": frames[0]["source_pts_seconds"],
                "actual_last_source_pts_seconds": frames[-1]["source_pts_seconds"],
                "distinct_pts_count": distinct_pts,
                "duplicate_pts_count": duplicate_pts,
                "pipeline_drop_count": 0,
                "max_frames_in_any_window": max_window_count,
                "density_window_seconds": window_seconds,
            }

        source_hash_after = _sha256(source)
        if source_hash_after != source_hash_before:
            raise RuntimeError("Source changed during extraction")
        frames_tree_sha256 = _frames_tree_hash(frames)
        manifest = {
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "pipeline_provenance": {
                "script_sha256": _sha256(Path(__file__).resolve()),
                "ffmpeg_version": _ffmpeg_version(),
                "filter_policy": ",".join(filters),
                "protocol_whitelist": ["file", "pipe"],
                "forced_input_demuxer": input_demuxer,
            },
            "source": {
                "path": str(source),
                "sha256": source_hash_before,
                "hash_stable_during_extraction": True,
                **probe,
            },
            "settings": {
                "mode": mode,
                "start_seconds": start_seconds,
                "duration_seconds": duration_seconds,
                "baseline_fps": baseline_fps,
                "max_native_fps": max_native_fps,
                "burst_trigger": burst_trigger,
                "long_edge": long_edge,
                "max_frames": max_frames,
                "resize_policy": "fit_without_upscale_preserve_aspect_even_dimensions",
                "image_codec": "jpeg",
                "ffmpeg_quality": 3,
                "output_pixel_format": "yuvj420p",
                "interpolation_allowed": False,
                "baseline_selection_policy": (
                    "first_decoded_frame_at_or_after_each_target"
                    if mode == "baseline"
                    else None
                ),
            },
            "frames_tree_sha256": frames_tree_sha256,
            "burst_summary": burst_summary,
            "frames": frames,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _validate_staged_manifest(temporary, manifest)
        os.rename(temporary, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def run_cli(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Extract one bounded visual chunk from an authorised local file."
    )
    parser.add_argument("source")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mode", choices=("baseline", "native-burst"), required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--baseline-fps", type=int)
    parser.add_argument("--max-native-fps", type=int)
    parser.add_argument("--burst-trigger")
    parser.add_argument("--long-edge", type=int, default=448)
    parser.add_argument("--max-frames", type=int, required=True)
    args = parser.parse_args(argv)
    return run_visual_chunk(
        Path(args.source),
        Path(args.output_dir),
        mode=args.mode,
        start_seconds=args.start,
        duration_seconds=args.duration,
        baseline_fps=args.baseline_fps,
        max_native_fps=args.max_native_fps,
        burst_trigger=args.burst_trigger,
        long_edge=args.long_edge,
        max_frames=args.max_frames,
    )


if __name__ == "__main__":
    run_cli(os.sys.argv[1:])
