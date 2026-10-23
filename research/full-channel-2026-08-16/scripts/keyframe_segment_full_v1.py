from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable


SCHEMA = "keyframe-aligned-full-source-segments-v1"
MAX_SECTION_SECONDS = Decimal("600.000000")
DURATION_TOLERANCE = Decimal("0.250000")


def q6(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def plan_keyframe_sections(
    duration_seconds: Decimal,
    keyframes: Iterable[Decimal],
    max_section_seconds: Decimal = MAX_SECTION_SECONDS,
) -> list[dict[str, str | int]]:
    if duration_seconds <= 0 or max_section_seconds <= 0:
        raise ValueError("duration and section cap must be positive")
    ordered = sorted(
        {point for point in keyframes if Decimal("0") <= point < duration_seconds}
    )
    sections: list[dict[str, str | int]] = []
    start = Decimal("0")
    while duration_seconds - start > max_section_seconds:
        limit = start + max_section_seconds
        candidates = [point for point in ordered if start < point <= limit]
        if not candidates:
            raise RuntimeError(
                "no keyframe can close the current section within the bounded duration"
            )
        end = candidates[-1]
        sections.append(
            {
                "index": len(sections),
                "start_seconds": q6(start),
                "end_seconds": q6(end),
                "duration_seconds": q6(end - start),
            }
        )
        start = end
    sections.append(
        {
            "index": len(sections),
            "start_seconds": q6(start),
            "end_seconds": q6(duration_seconds),
            "duration_seconds": q6(duration_seconds - start),
        }
    )
    previous_end = Decimal("0")
    for section in sections:
        start_value = Decimal(str(section["start_seconds"]))
        end_value = Decimal(str(section["end_seconds"]))
        section_duration = Decimal(str(section["duration_seconds"]))
        if start_value != previous_end or end_value <= start_value:
            raise RuntimeError("planned source-time coverage has a gap or overlap")
        if section_duration != end_value - start_value:
            raise RuntimeError("planned section duration is inconsistent")
        if section_duration > max_section_seconds:
            raise RuntimeError("planned section exceeds the bounded duration")
        previous_end = end_value
    if previous_end != duration_seconds:
        raise RuntimeError("planned source-time coverage is incomplete")
    return sections


def _run_json(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")
        raise RuntimeError(f"command failed with exit {completed.returncode}: {detail[:500]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("command returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("command returned an invalid JSON root")
    return value


def probe_video(source: Path) -> dict[str, object]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is unavailable")
    raw = _run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration,start_time:stream=codec_name,profile,width,height,pix_fmt,avg_frame_rate,start_time",
            "-of",
            "json",
            str(source),
        ]
    )
    streams = raw.get("streams")
    fmt = raw.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(fmt, dict):
        raise RuntimeError("source must expose exactly one selected video stream")
    duration = fmt.get("duration")
    try:
        duration_value = Decimal(str(duration))
    except InvalidOperation:
        raise RuntimeError("source duration is invalid") from None
    if duration_value <= 0:
        raise RuntimeError("source duration is invalid")
    return {
        "duration_seconds": q6(duration_value),
        "format_start_seconds": str(fmt.get("start_time", "0.000000")),
        "video_stream": streams[0],
    }


def read_keyframes(source: Path) -> list[Decimal]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is unavailable")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-skip_frame",
            "nokey",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError("ffprobe keyframe scan failed")
    keyframes: list[Decimal] = []
    for line in completed.stdout.splitlines():
        token = line.strip().split(",")[0]
        if not token:
            continue
        try:
            keyframes.append(Decimal(token))
        except InvalidOperation:
            raise RuntimeError("ffprobe returned an invalid keyframe timestamp") from None
    if not keyframes:
        raise RuntimeError("ffprobe returned no keyframes")
    return keyframes


def _segment_command(source: Path, output: Path, section: dict[str, object]) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        str(section["start_seconds"]),
        "-i",
        str(source),
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


def _tree_hash(root: Path, excluded: set[str] | None = None) -> str:
    excluded = excluded or set()
    rows: list[str] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda p: p.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        rows.append(f"{relative}\0{path.stat().st_size}\0{sha256_file(path)}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest().upper()


def verify_output(output_dir: Path) -> dict[str, object]:
    plan_path = output_dir / "plan.json"
    completion_path = output_dir / "completion.json"
    if not plan_path.is_file() or not completion_path.is_file():
        raise RuntimeError("segmentation output is incomplete")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    if plan.get("schema") != SCHEMA or completion.get("schema") != f"{SCHEMA}-completion":
        raise RuntimeError("segmentation schema mismatch")
    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("segmentation plan is empty")
    expected_dirs = {f"segment-{index:05d}" for index in range(len(sections))}
    actual_dirs = {path.name for path in output_dir.iterdir() if path.is_dir()}
    if actual_dirs != expected_dirs:
        raise RuntimeError("segment directory set mismatch")
    for section in sections:
        index = int(section["index"])
        directory = output_dir / f"segment-{index:05d}"
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        media = directory / str(manifest["media"]["file"])
        if set(path.name for path in directory.iterdir()) != {"manifest.json", media.name}:
            raise RuntimeError("segment file set mismatch")
        if sha256_file(media) != manifest["media"]["sha256"]:
            raise RuntimeError("segment media hash mismatch")
        actual = Decimal(str(manifest["media"]["probe"]["duration_seconds"]))
        requested = Decimal(str(section["duration_seconds"]))
        if abs(actual - requested) > DURATION_TOLERANCE:
            raise RuntimeError("segment duration verification failed")
        if actual > MAX_SECTION_SECONDS + DURATION_TOLERANCE:
            raise RuntimeError("segment exceeds bounded duration")
    tree = _tree_hash(output_dir, {"completion.json"})
    if completion.get("tree_sha256") != tree:
        raise RuntimeError("segmentation tree hash mismatch")
    return completion


def build(source: Path, output_dir: Path) -> dict[str, object]:
    source = source.resolve()
    output_dir = output_dir.resolve()
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError("full source is missing")
    if output_dir.exists():
        raise RuntimeError("output directory already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.partial-", dir=output_dir.parent))
    committed = False
    try:
        source_probe = probe_video(source)
        source_duration = Decimal(str(source_probe["duration_seconds"]))
        keyframes = read_keyframes(source)
        sections = plan_keyframe_sections(source_duration, keyframes)
        plan = {
            "schema": SCHEMA,
            "policy": {
                "max_section_seconds": q6(MAX_SECTION_SECONDS),
                "duration_tolerance_seconds": q6(DURATION_TOLERANCE),
                "video_only": True,
                "stream_copy": True,
                "reencode": False,
                "source_time_coverage": "contiguous-complete",
            },
            "source": {
                "path": str(source),
                "bytes": source.stat().st_size,
                "sha256": sha256_file(source),
                "probe": source_probe,
            },
            "keyframe_count": len(keyframes),
            "sections": sections,
        }
        (staging / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for section in sections:
            index = int(section["index"])
            directory = staging / f"segment-{index:05d}"
            directory.mkdir()
            media = directory / "media.mp4"
            completed = subprocess.run(
                _segment_command(source, media, section),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                raise RuntimeError(f"segment {index} stream copy failed")
            segment_probe = probe_video(media)
            actual = Decimal(str(segment_probe["duration_seconds"]))
            requested = Decimal(str(section["duration_seconds"]))
            if abs(actual - requested) > DURATION_TOLERANCE:
                raise RuntimeError(f"segment {index} duration differs from keyframe plan")
            if actual > MAX_SECTION_SECONDS + DURATION_TOLERANCE:
                raise RuntimeError(f"segment {index} exceeds bounded duration")
            manifest = {
                "schema": f"{SCHEMA}-segment",
                "source_sha256": plan["source"]["sha256"],
                "section": section,
                "acquisition": {
                    "runner": "ffmpeg-local-keyframe-stream-copy-v1",
                    "video_only": True,
                    "reencode": False,
                    "atomic_no_clobber": True,
                },
                "media": {
                    "file": media.name,
                    "bytes": media.stat().st_size,
                    "sha256": sha256_file(media),
                    "probe": segment_probe,
                },
            }
            (directory / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        completion = {
            "schema": f"{SCHEMA}-completion",
            "source_sha256": plan["source"]["sha256"],
            "section_count": len(sections),
            "source_duration_seconds": plan["source"]["probe"]["duration_seconds"],
            "tree_sha256": _tree_hash(staging),
        }
        (staging / "completion.json").write_text(
            json.dumps(completion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.rename(staging, output_dir)
        committed = True
        return verify_output(output_dir)
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--source", required=True)
    build_parser.add_argument("--output-dir", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if args.action == "build":
        result = build(Path(args.source), Path(args.output_dir))
    else:
        result = verify_output(Path(args.output_dir).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
