from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from extract_visual_chunk import (
    DEMUXER_BY_EXTENSION,
    _ffmpeg_version,
    _scale_filter,
    _sha256,
    _validate_local_output_reference,
    _validate_local_source_reference,
)


PIPELINE_VERSION = "global-visual-lattice-v1"
SHOWINFO_RE = re.compile(
    r"n:\s*(?P<n>\d+).*?pts:\s*(?P<pts>[+\-]?\d+).*?"
    r"pts_time:(?P<pts_time>[+\-0-9.eE]+).*?"
    r"s:(?P<width>\d+)x(?P<height>\d+).*?checksum:(?P<checksum>[0-9A-F]+)"
)


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def plan_global_lattice(
    *,
    frame_pts: Iterable[Fraction],
    source_duration: Fraction,
    baseline_fps: int,
    sections: list[tuple[int, Fraction, Fraction]],
) -> list[dict[str, object]]:
    """Map every global n/f target to its first source frame and owning section."""
    if source_duration <= 0:
        raise ValueError("source_duration must be positive")
    if baseline_fps not in {3, 4, 5}:
        raise ValueError("baseline_fps must be 3, 4, or 5")
    points = list(frame_pts)
    if not points or points != sorted(points) or len(points) != len(set(points)):
        raise ValueError("frame PTS must be non-empty, unique, and sorted")
    if points[0] < 0 or points[-1] >= source_duration:
        raise ValueError("frame PTS must be inside the playable interval")
    if not sections:
        raise ValueError("sections are required")
    ordered = sorted(sections, key=lambda row: row[1])
    if ordered != sections or ordered[0][1] != 0 or ordered[-1][2] != source_duration:
        raise ValueError("sections must cover the source in order")
    for position, (index, start, end) in enumerate(ordered):
        if index != position or start >= end:
            raise ValueError("sections must use contiguous indices and positive ranges")
        if position and ordered[position - 1][2] != start:
            raise ValueError("sections must be contiguous")

    starts = [row[1] for row in ordered]
    target_count = _ceil_fraction(source_duration * baseline_fps)
    rows: list[dict[str, object]] = []
    frame_index = 0
    previous_selected: Fraction | None = None
    for target_index in range(target_count):
        target = Fraction(target_index, baseline_fps)
        while frame_index < len(points) and points[frame_index] < target:
            frame_index += 1
        if frame_index >= len(points):
            raise RuntimeError("global target has no source frame at or after it")
        selected = points[frame_index]
        if previous_selected is not None and selected <= previous_selected:
            raise RuntimeError("global targets do not map to unique monotonic source PTS")
        owner_position = bisect_right(starts, selected) - 1
        if owner_position < 0:
            raise RuntimeError("selected frame has no owning section")
        owner_index, owner_start, owner_end = ordered[owner_position]
        if not (owner_start <= selected < owner_end):
            raise RuntimeError("selected frame has no owning section")
        rows.append(
            {
                "target_index": target_index,
                "target_seconds": target,
                "selected_source_pts_seconds": selected,
                "owner_section_index": owner_index,
            }
        )
        previous_selected = selected

    return rows


def resolve_plan_sections(
    *,
    plan_rows: list[dict[str, object]],
    frame_pts: list[Fraction],
    source_duration: Fraction,
    tolerance: Fraction = Fraction(1, 1_000_000),
) -> list[tuple[int, Fraction, Fraction]]:
    """Resolve rounded plan boundaries to exact source-frame PTS."""
    if not plan_rows or not frame_pts or tolerance < 0:
        raise ValueError("plan rows, frame PTS, and non-negative tolerance are required")

    def resolve(value: Fraction) -> Fraction:
        if abs(value) <= tolerance:
            return Fraction(0)
        if abs(value - source_duration) <= tolerance:
            return source_duration
        position = bisect_right(frame_pts, value)
        candidates = []
        if position:
            candidates.append(frame_pts[position - 1])
        if position < len(frame_pts):
            candidates.append(frame_pts[position])
        if not candidates:
            raise RuntimeError("plan boundary has no source-frame candidate")
        candidate = min(candidates, key=lambda row: abs(row - value))
        if abs(candidate - value) > tolerance:
            raise RuntimeError("plan boundary does not resolve to source-frame PTS")
        return candidate

    sections: list[tuple[int, Fraction, Fraction]] = []
    for position, row in enumerate(plan_rows):
        if int(row["index"]) != position:
            raise ValueError("plan section indices must be contiguous")
        start = resolve(Fraction(str(row["start_seconds"])))
        end = resolve(Fraction(str(row["end_seconds"])))
        if start >= end or (position and sections[-1][2] != start):
            raise RuntimeError("resolved plan sections are not contiguous")
        sections.append((position, start, end))
    if sections[0][1] != 0 or sections[-1][2] != source_duration:
        raise RuntimeError("resolved plan does not cover the source")
    return sections


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _decimal_text(value: Fraction, places: int = 12) -> str:
    return f"{float(value):.{places}f}"


def _ffmpeg_fraction_expression(value: Fraction) -> str:
    return f"({value.numerator}/{value.denominator})"


def _run_json(command: list[str]) -> dict:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError("ffprobe failed for the local source")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe returned invalid JSON") from None


def probe_global_source(source: Path) -> dict[str, object]:
    """Read the complete local video PTS lattice without decoding audio."""
    source = _validate_local_source_reference(Path(source))
    if not source.is_file():
        raise FileNotFoundError(source)
    demuxer = DEMUXER_BY_EXTENSION[source.suffix.lower()]
    payload = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            demuxer,
            "-show_streams",
            "-show_format",
            "-show_frames",
            "-show_entries",
            "stream=index,codec_type,disposition,time_base,duration_ts,duration,start_time,avg_frame_rate,r_frame_rate,width,height,codec_name,pix_fmt:format=duration,start_time:frame=best_effort_timestamp,pts",
            "-of",
            "json",
            str(source),
        ]
    )
    stream = next(
        (
            row
            for row in payload.get("streams", [])
            if row.get("codec_type") == "video"
            and row.get("disposition", {}).get("attached_pic", 0) != 1
        ),
        None,
    )
    if stream is None:
        raise ValueError("local source has no video stream")
    stream_start = Fraction(stream.get("start_time", "0"))
    format_start = Fraction(payload.get("format", {}).get("start_time", "0"))
    if stream_start != 0 or format_start != 0:
        raise RuntimeError("non-zero timestamp origin is unsupported")
    time_base = Fraction(stream["time_base"])
    if stream.get("duration_ts") is not None:
        duration = Fraction(int(stream["duration_ts"])) * time_base
    elif stream.get("duration") is not None:
        duration = Fraction(stream["duration"])
    else:
        duration = Fraction(payload.get("format", {}).get("duration", "0"))
    if duration <= 0:
        raise RuntimeError("source duration is unavailable")
    raw_pts: list[int] = []
    for frame in payload.get("frames", []):
        raw = frame.get("best_effort_timestamp")
        if raw is None:
            raw = frame.get("pts")
        if raw is None:
            raise RuntimeError("source frame PTS is unavailable")
        raw_pts.append(int(raw))
    frame_pts = [Fraction(raw) * time_base for raw in raw_pts]
    if (
        not frame_pts
        or frame_pts != sorted(frame_pts)
        or len(frame_pts) != len(set(frame_pts))
        or frame_pts[0] < 0
        or frame_pts[-1] >= duration
    ):
        raise RuntimeError("source frame PTS lattice is invalid")
    return {
        "path": source,
        "sha256": _sha256(source),
        "bytes": source.stat().st_size,
        "demuxer": demuxer,
        "duration": duration,
        "time_base": time_base,
        "frame_pts_raw": raw_pts,
        "frame_pts": frame_pts,
        "stream_index": int(stream["index"]),
        "codec_name": stream.get("codec_name"),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "pixel_format": stream.get("pix_fmt"),
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "r_frame_rate": stream.get("r_frame_rate"),
    }


def write_global_contract(
    *,
    source: Path,
    output_dir: Path,
    plan_rows: list[dict[str, object]],
    baseline_fps: int,
    long_edge: int,
    plan_sha256: str,
    research_contract_sha256: str,
) -> dict:
    """Atomically publish an exact global target ledger and section ownership contract."""
    source = _validate_local_source_reference(Path(source))
    output_dir = _validate_local_output_reference(Path(output_dir))
    if output_dir.exists():
        raise FileExistsError(output_dir)
    if not re.fullmatch(r"[0-9A-F]{64}", plan_sha256):
        raise ValueError("plan_sha256 must be uppercase SHA-256")
    if not re.fullmatch(r"[0-9A-F]{64}", research_contract_sha256):
        raise ValueError("research_contract_sha256 must be uppercase SHA-256")
    if not (64 <= long_edge <= 4096):
        raise ValueError("long_edge must be between 64 and 4096")
    probe = probe_global_source(source)
    source_hash_before = str(probe["sha256"])
    sections = resolve_plan_sections(
        plan_rows=plan_rows,
        frame_pts=list(probe["frame_pts"]),
        source_duration=Fraction(probe["duration"]),
    )
    rows = plan_global_lattice(
        frame_pts=list(probe["frame_pts"]),
        source_duration=Fraction(probe["duration"]),
        baseline_fps=baseline_fps,
        sections=sections,
    )
    indices = [int(row["target_index"]) for row in rows]
    selected_pts = [Fraction(row["selected_source_pts_seconds"]) for row in rows]
    if indices != list(range(len(rows))) or len(indices) != len(set(indices)):
        raise RuntimeError("global target index lattice has a gap or duplicate")
    if len(selected_pts) != len(set(selected_pts)):
        raise RuntimeError("global target lattice selected duplicate source PTS")

    ledger_rows = [
        {
            "target_index": int(row["target_index"]),
            "target_fraction": _fraction_text(Fraction(row["target_seconds"])),
            "target_seconds": round(float(Fraction(row["target_seconds"])), 9),
            "selected_source_pts_fraction": _fraction_text(
                Fraction(row["selected_source_pts_seconds"])
            ),
            "selected_source_pts_seconds": round(
                float(Fraction(row["selected_source_pts_seconds"])), 9
            ),
            "owner_section_index": int(row["owner_section_index"]),
        }
        for row in rows
    ]
    section_records = []
    for section_index, start, end in sections:
        owned = [row for row in ledger_rows if row["owner_section_index"] == section_index]
        if not owned:
            raise RuntimeError("global section owns no targets")
        owned_indices = [row["target_index"] for row in owned]
        if owned_indices != list(range(owned_indices[0], owned_indices[-1] + 1)):
            raise RuntimeError("global section target indices are not contiguous")
        section_records.append(
            {
                "index": section_index,
                "start_fraction": _fraction_text(start),
                "start_seconds": round(float(start), 9),
                "end_fraction": _fraction_text(end),
                "end_seconds": round(float(end), 9),
                "ownership": "start_inclusive_end_exclusive_by_selected_source_pts",
                "first_target_index": owned_indices[0],
                "last_target_index": owned_indices[-1],
                "target_count": len(owned),
            }
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent)
    )
    try:
        ledger_path = staging / "target-ledger.jsonl"
        ledger_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger_rows),
            encoding="utf-8",
            newline="\n",
        )
        source_hash_after = _sha256(source)
        if source_hash_after != source_hash_before:
            raise RuntimeError("source changed during global contract planning")
        contract = {
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "pipeline_provenance": {
                "script_sha256": _sha256(Path(__file__).resolve()),
                "python_version": os.sys.version.split()[0],
                "ffprobe_policy": "complete_video_frame_pts_video_stream_only",
                "protocol_whitelist": ["file", "pipe"],
            },
            "source": {
                "path": str(source),
                "sha256": source_hash_before,
                "bytes": probe["bytes"],
                "video_only_analysis": True,
                "time_base": _fraction_text(Fraction(probe["time_base"])),
                "duration_fraction": _fraction_text(Fraction(probe["duration"])),
                "duration_seconds": round(float(Fraction(probe["duration"])), 9),
                "frame_pts_count": len(probe["frame_pts"]),
                "codec_name": probe["codec_name"],
                "width": probe["width"],
                "height": probe["height"],
                "pixel_format": probe["pixel_format"],
                "avg_frame_rate": probe["avg_frame_rate"],
                "r_frame_rate": probe["r_frame_rate"],
                "stream_index": probe["stream_index"],
                "hash_stable_during_planning": True,
            },
            "settings": {
                "baseline_fps": baseline_fps,
                "global_target_formula": "target_index / baseline_fps",
                "target_endpoint": "target_less_than_source_duration",
                "selection_policy": "first_source_frame_at_or_after_global_target",
                "section_ownership": "start_inclusive_end_exclusive_by_selected_source_pts",
                "boundary_frame_rule": "exact_end_boundary_belongs_to_next_section",
                "long_edge": long_edge,
                "interpolation_allowed": False,
                "publication": "atomic_create_new_only",
                "plan_sha256": plan_sha256,
                "research_contract_sha256": research_contract_sha256,
            },
            "global_lattice": {
                "target_count": len(rows),
                "first_target_index": indices[0],
                "last_target_index": indices[-1],
                "first_target_fraction": ledger_rows[0]["target_fraction"],
                "last_target_fraction": ledger_rows[-1]["target_fraction"],
                "target_step_fraction": f"1/{baseline_fps}",
                "zero_gap_target_indices": indices == list(range(len(rows))),
                "zero_duplicate_target_indices": len(indices) == len(set(indices)),
                "zero_duplicate_selected_pts": len(selected_pts) == len(set(selected_pts)),
                "target_ledger_sha256": _sha256(ledger_path),
            },
            "sections": section_records,
        }
        (staging / "contract.json").write_text(
            json.dumps(contract, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        actual_files = {
            candidate.relative_to(staging).as_posix()
            for candidate in staging.rglob("*")
            if candidate.is_file()
        }
        if actual_files != {"contract.json", "target-ledger.jsonl"}:
            raise RuntimeError("staged global contract file set mismatch")
        os.rename(staging, output_dir)
        return contract
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _frames_tree_hash(frames: list[dict]) -> str:
    material = "\n".join(
        f"{row['relative_path']}\0{row['sha256']}\0{row['global_target_index']}\0{row['source_pts_fraction']}"
        for row in frames
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def run_global_section_extract(
    *,
    source: Path,
    output_dir: Path,
    section: tuple[int, Fraction, Fraction],
    expected_rows: list[dict[str, object]],
    baseline_fps: int,
    long_edge: int,
    max_frames: int,
    source_probe: dict[str, object] | None = None,
) -> dict:
    """Atomically extract one section's globally anchored target rows."""
    source = _validate_local_source_reference(Path(source))
    output_dir = _validate_local_output_reference(Path(output_dir))
    if output_dir.exists():
        raise FileExistsError(output_dir)
    if baseline_fps not in {3, 4, 5}:
        raise ValueError("baseline_fps must be 3, 4, or 5")
    if not (64 <= long_edge <= 4096):
        raise ValueError("long_edge must be between 64 and 4096")
    if not expected_rows or len(expected_rows) > max_frames:
        raise ValueError("expected global rows must be non-empty and bounded")
    section_index, section_start, section_end = section
    if section_start < 0 or section_end <= section_start:
        raise ValueError("invalid section range")
    expected_indices = [int(row["target_index"]) for row in expected_rows]
    if expected_indices != list(range(expected_indices[0], expected_indices[-1] + 1)):
        raise ValueError("section global target indices must be contiguous")
    if any(int(row["owner_section_index"]) != section_index for row in expected_rows):
        raise ValueError("section contains a target owned by another section")
    if any(
        row["target_seconds"] != Fraction(row["target_index"], baseline_fps)
        for row in expected_rows
    ):
        raise ValueError("global target lattice is not n/f")

    probe = source_probe if source_probe is not None else probe_global_source(source)
    if Path(probe["path"]).resolve() != source.resolve():
        raise ValueError("source probe path mismatch")
    source_hash_before = _sha256(source)
    if source_hash_before != probe["sha256"]:
        raise RuntimeError("source hash drifted after global probe")
    time_base = Fraction(probe["time_base"])
    frame_pts = list(probe["frame_pts"])
    try:
        section_start_raw = probe["frame_pts_raw"][frame_pts.index(section_start)]
    except ValueError:
        raise RuntimeError("section start is not an exact source frame PTS") from None
    if section_end > Fraction(probe["duration"]):
        raise ValueError("section exceeds source duration")
    raw_duration = (section_end - section_start) / time_base
    if raw_duration.denominator != 1:
        raise RuntimeError("section duration is not integral in the source time base")
    section_duration_raw = raw_duration.numerator

    first_relative_target = Fraction(expected_rows[0]["target_seconds"]) - section_start
    first_target_ticks = first_relative_target / time_base
    target_step_ticks = Fraction(1, baseline_fps) / time_base
    section_duration = section_end - section_start
    demuxer = str(probe["demuxer"])
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent)
    )
    frames_dir = temporary / "frames"
    frames_dir.mkdir()
    try:
        filters = [
            "select='"
            f"lt(pts,{section_duration_raw})*"
            f"gte(pts,{_ffmpeg_fraction_expression(first_target_ticks)}+"
            f"selected_n*{_ffmpeg_fraction_expression(target_step_ticks)})"
            "'",
            _scale_filter(long_edge),
            "showinfo",
        ]
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-ss",
            _decimal_text(section_start),
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            demuxer,
            "-i",
            str(source),
            "-t",
            _decimal_text(section_duration),
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
            raise RuntimeError("FFmpeg global section extraction failed")
        showinfo: dict[int, dict[str, object]] = {}
        for line in completed.stderr.splitlines():
            match = SHOWINFO_RE.search(line)
            if match:
                showinfo[int(match.group("n"))] = {
                    "relative_pts_raw": int(match.group("pts")),
                    "relative_pts_seconds": Fraction(match.group("pts_time")),
                    "width": int(match.group("width")),
                    "height": int(match.group("height")),
                    "processed_frame_checksum": match.group("checksum"),
                }
        images = sorted(frames_dir.glob("frame_*.jpg"))
        if len(images) != len(expected_rows) or len(showinfo) != len(expected_rows):
            raise RuntimeError(
                "global target coverage count mismatch: "
                f"expected={len(expected_rows)} images={len(images)} showinfo={len(showinfo)} "
                f"section={section_index}"
            )

        frames: list[dict] = []
        for sequence, (image_path, expected) in enumerate(zip(images, expected_rows)):
            evidence = showinfo.get(sequence)
            if evidence is None:
                raise RuntimeError("missing PTS evidence for global target")
            absolute_raw = int(section_start_raw) + int(evidence["relative_pts_raw"])
            absolute_pts = Fraction(absolute_raw) * time_base
            if absolute_pts != expected["selected_source_pts_seconds"]:
                raise RuntimeError("decoded source PTS disagrees with global lattice contract")
            target = Fraction(expected["target_seconds"])
            if not (section_start <= absolute_pts < section_end):
                raise RuntimeError("decoded frame violates section ownership")
            frames.append(
                {
                    "sequence": sequence,
                    "relative_path": f"frames/{image_path.name}",
                    "global_target_index": int(expected["target_index"]),
                    "global_target_fraction": _fraction_text(target),
                    "sample_target_seconds": round(float(target), 9),
                    "source_pts_fraction": _fraction_text(absolute_pts),
                    "source_pts_seconds": round(float(absolute_pts), 9),
                    "sampling_error_seconds": round(float(absolute_pts - target), 9),
                    "owner_section_index": section_index,
                    "relative_pts_raw": int(evidence["relative_pts_raw"]),
                    "width": int(evidence["width"]),
                    "height": int(evidence["height"]),
                    "processed_frame_checksum": evidence["processed_frame_checksum"],
                    "sha256": _sha256(image_path),
                }
            )
        if len({row["source_pts_fraction"] for row in frames}) != len(frames):
            raise RuntimeError("duplicate source PTS in global section")
        source_hash_after = _sha256(source)
        if source_hash_after != source_hash_before:
            raise RuntimeError("source changed during global section extraction")
        manifest = {
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "pipeline_provenance": {
                "script_sha256": _sha256(Path(__file__).resolve()),
                "ffmpeg_version": _ffmpeg_version(),
                "filter_policy": ",".join(filters),
                "protocol_whitelist": ["file", "pipe"],
                "forced_input_demuxer": demuxer,
            },
            "source": {
                "path": str(source),
                "sha256": source_hash_before,
                "bytes": source.stat().st_size,
                "hash_stable_during_extraction": True,
                "time_base": _fraction_text(time_base),
                "duration_fraction": _fraction_text(Fraction(probe["duration"])),
                "duration_seconds": round(float(Fraction(probe["duration"])), 9),
                "codec_name": probe["codec_name"],
                "width": probe["width"],
                "height": probe["height"],
                "pixel_format": probe["pixel_format"],
                "avg_frame_rate": probe["avg_frame_rate"],
                "r_frame_rate": probe["r_frame_rate"],
                "stream_index": probe["stream_index"],
            },
            "settings": {
                "mode": "global-baseline",
                "baseline_fps": baseline_fps,
                "global_target_formula": "target_index / baseline_fps",
                "section_index": section_index,
                "section_start_fraction": _fraction_text(section_start),
                "section_end_fraction": _fraction_text(section_end),
                "section_start_seconds": round(float(section_start), 9),
                "section_end_seconds": round(float(section_end), 9),
                "section_ownership": "start_inclusive_end_exclusive_by_selected_source_pts",
                "boundary_frame_rule": "exact_end_boundary_belongs_to_next_section",
                "first_global_target_index": expected_indices[0],
                "last_global_target_index": expected_indices[-1],
                "long_edge": long_edge,
                "image_codec": "jpeg",
                "ffmpeg_quality": 3,
                "interpolation_allowed": False,
                "publication": "atomic-create-new-only",
            },
            "frames_tree_sha256": _frames_tree_hash(frames),
            "frames": frames,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        expected_files = {"manifest.json"} | {
            row["relative_path"] for row in frames
        }
        actual_files = {
            candidate.relative_to(temporary).as_posix()
            for candidate in temporary.rglob("*")
            if candidate.is_file()
        }
        if actual_files != expected_files:
            raise RuntimeError("staged global section file set mismatch")
        for row in frames:
            candidate = temporary / row["relative_path"]
            if _sha256(candidate) != row["sha256"]:
                raise RuntimeError("staged global section frame hash mismatch")
        os.rename(temporary, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_ledger_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stored = json.loads(line)
        rows.append(
            {
                "target_index": int(stored["target_index"]),
                "target_seconds": Fraction(stored["target_fraction"]),
                "selected_source_pts_seconds": Fraction(
                    stored["selected_source_pts_fraction"]
                ),
                "owner_section_index": int(stored["owner_section_index"]),
            }
        )
    return rows


def _validate_published_section(
    chunk_dir: Path,
    *,
    source_sha256: str,
    expected_rows: list[dict[str, object]],
    section_index: int,
) -> dict:
    try:
        manifest = json.loads((chunk_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(f"published global section is invalid: {chunk_dir.name}") from None
    if (
        manifest.get("pipeline_version") != PIPELINE_VERSION
        or manifest.get("source", {}).get("sha256") != source_sha256
        or manifest.get("settings", {}).get("section_index") != section_index
    ):
        raise RuntimeError(f"published global section contract mismatch: {chunk_dir.name}")
    frames = manifest.get("frames")
    if not isinstance(frames, list) or len(frames) != len(expected_rows):
        raise RuntimeError(f"published global section frame count mismatch: {chunk_dir.name}")
    if [row.get("global_target_index") for row in frames] != [
        row["target_index"] for row in expected_rows
    ]:
        raise RuntimeError(f"published global section target mismatch: {chunk_dir.name}")
    if [Fraction(row.get("source_pts_fraction", "0")) for row in frames] != [
        row["selected_source_pts_seconds"] for row in expected_rows
    ]:
        raise RuntimeError(f"published global section PTS mismatch: {chunk_dir.name}")
    expected_files = {"manifest.json"} | {row["relative_path"] for row in frames}
    actual_files = {
        candidate.relative_to(chunk_dir).as_posix()
        for candidate in chunk_dir.rglob("*")
        if candidate.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError(f"published global section file set mismatch: {chunk_dir.name}")
    for row in frames:
        if _sha256(chunk_dir / row["relative_path"]) != row["sha256"]:
            raise RuntimeError(f"published global section frame hash mismatch: {chunk_dir.name}")
    if _frames_tree_hash(frames) != manifest.get("frames_tree_sha256"):
        raise RuntimeError(f"published global section tree mismatch: {chunk_dir.name}")
    return manifest


def run_all_global_sections(
    *,
    source: Path,
    visual_root: Path,
    workers: int = 2,
) -> dict:
    """Resume or create all contract sections, then atomically publish completion."""
    source = _validate_local_source_reference(Path(source))
    visual_root = _validate_local_output_reference(Path(visual_root))
    if not visual_root.is_dir():
        raise FileNotFoundError("global visual contract root does not exist")
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    completion_path = visual_root / "completion.json"
    if completion_path.exists():
        raise FileExistsError(completion_path)
    contract_path = visual_root / "contract.json"
    ledger_path = visual_root / "target-ledger.jsonl"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if (
        contract.get("pipeline_version") != PIPELINE_VERSION
        or _sha256(ledger_path)
        != contract.get("global_lattice", {}).get("target_ledger_sha256")
    ):
        raise RuntimeError("global contract or target ledger integrity failed")
    probe = probe_global_source(source)
    if (
        probe["sha256"] != contract.get("source", {}).get("sha256")
        or str(source.resolve()) != str(Path(contract["source"]["path"]).resolve())
    ):
        raise RuntimeError("global contract source preimage mismatch")
    baseline_fps = int(contract["settings"]["baseline_fps"])
    long_edge = int(contract["settings"]["long_edge"])
    sections = [
        (
            int(row["index"]),
            Fraction(row["start_fraction"]),
            Fraction(row["end_fraction"]),
        )
        for row in contract["sections"]
    ]
    ledger_rows = _load_ledger_rows(ledger_path)
    recomputed = plan_global_lattice(
        frame_pts=list(probe["frame_pts"]),
        source_duration=Fraction(probe["duration"]),
        baseline_fps=baseline_fps,
        sections=sections,
    )
    if recomputed != ledger_rows:
        raise RuntimeError("global target ledger does not match current source PTS")

    chunks_root = visual_root / "chunks"
    chunks_root.mkdir(exist_ok=True)
    expected_chunk_names = {f"chunk-{row[0]:05d}" for row in sections}
    actual_chunk_names = {row.name for row in chunks_root.iterdir() if row.is_dir()}
    unexpected = actual_chunk_names - expected_chunk_names
    if unexpected:
        raise RuntimeError("unexpected global chunk directories")

    section_inputs = []
    manifests: dict[int, dict] = {}
    for section in sections:
        section_index = section[0]
        expected = [
            row for row in ledger_rows if row["owner_section_index"] == section_index
        ]
        chunk_dir = chunks_root / f"chunk-{section_index:05d}"
        if chunk_dir.exists():
            manifests[section_index] = _validate_published_section(
                chunk_dir,
                source_sha256=str(probe["sha256"]),
                expected_rows=expected,
                section_index=section_index,
            )
        else:
            section_inputs.append((section, expected, chunk_dir))

    def extract_one(item: tuple[tuple[int, Fraction, Fraction], list[dict], Path]):
        section, expected, chunk_dir = item
        return section[0], run_global_section_extract(
            source=source,
            output_dir=chunk_dir,
            section=section,
            expected_rows=expected,
            baseline_fps=baseline_fps,
            long_edge=long_edge,
            max_frames=len(expected) + 2,
            source_probe=probe,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(extract_one, item) for item in section_inputs]
        for future in as_completed(futures):
            section_index, manifest = future.result()
            manifests[section_index] = manifest

    if set(manifests) != {row[0] for row in sections}:
        raise RuntimeError("global section set is incomplete")
    ordered_manifests = [manifests[index] for index in sorted(manifests)]
    all_frames = [frame for manifest in ordered_manifests for frame in manifest["frames"]]
    all_indices = [int(row["global_target_index"]) for row in all_frames]
    all_pts = [Fraction(row["source_pts_fraction"]) for row in all_frames]
    target_count = int(contract["global_lattice"]["target_count"])
    zero_gap = all_indices == list(range(target_count))
    zero_duplicate_targets = len(all_indices) == len(set(all_indices))
    zero_duplicate_pts = len(all_pts) == len(set(all_pts))
    if (
        len(all_frames) != target_count
        or not zero_gap
        or not zero_duplicate_targets
        or not zero_duplicate_pts
        or all_pts != sorted(all_pts)
    ):
        raise RuntimeError("global extraction completion lattice failed")
    manifest_records = []
    for section_index in sorted(manifests):
        manifest_path = chunks_root / f"chunk-{section_index:05d}" / "manifest.json"
        manifest_records.append(
            {
                "section_index": section_index,
                "relative_path": manifest_path.relative_to(visual_root).as_posix(),
                "sha256": _sha256(manifest_path),
                "frames_tree_sha256": manifests[section_index]["frames_tree_sha256"],
                "frame_count": len(manifests[section_index]["frames"]),
            }
        )
    tree_material = "\n".join(
        f"{row['relative_path']}\0{row['sha256']}\0{row['frames_tree_sha256']}"
        for row in manifest_records
    ).encode("utf-8")
    completion = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "source_sha256": probe["sha256"],
        "contract_sha256": _sha256(contract_path),
        "target_ledger_sha256": _sha256(ledger_path),
        "section_count": len(sections),
        "global_target_count": target_count,
        "extracted_frame_count": len(all_frames),
        "first_target_index": all_indices[0],
        "last_target_index": all_indices[-1],
        "first_target_fraction": all_frames[0]["global_target_fraction"],
        "last_target_fraction": all_frames[-1]["global_target_fraction"],
        "zero_gap_target_indices": zero_gap,
        "zero_duplicate_target_indices": zero_duplicate_targets,
        "zero_duplicate_source_pts": zero_duplicate_pts,
        "strictly_monotonic_source_pts": all_pts == sorted(all_pts),
        "chunk_manifest_tree_sha256": hashlib.sha256(tree_material).hexdigest().upper(),
        "chunks": manifest_records,
        "generator_script_sha256": _sha256(Path(__file__).resolve()),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".completion.json.tmp-", dir=visual_root
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_text(
            json.dumps(completion, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if completion_path.exists():
            raise FileExistsError(completion_path)
        os.rename(temporary_path, completion_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return completion


def _contact_tile(
    *,
    image_path: Path,
    tile_width: int,
    tile_height: int,
    label: str,
) -> Image.Image:
    """Render one evidence frame without cropping it or inventing image content."""
    with Image.open(image_path) as opened:
        source = opened.convert("RGB")
    source.thumbnail((tile_width, tile_height), Image.Resampling.LANCZOS)
    tile = Image.new("RGB", (tile_width, tile_height), "black")
    left = (tile_width - source.width) // 2
    top = (tile_height - source.height) // 2
    tile.paste(source, (left, top))

    draw = ImageDraw.Draw(tile)
    font = ImageFont.load_default()
    label_height = max(14, min(30, tile_height // 6))
    draw.rectangle(
        (0, tile_height - label_height, tile_width, tile_height),
        fill=(0, 0, 0),
    )
    draw.text(
        (4, tile_height - label_height + 2),
        label,
        font=font,
        fill=(255, 255, 255),
    )
    return tile


def build_global_contact_sheets(
    *,
    visual_root: Path,
    output_dir: Path,
    tile_width: int = 448,
    tile_height: int = 252,
) -> dict:
    """Atomically build unviewed sheets from one completed global-only tree."""
    visual_root = _validate_local_output_reference(Path(visual_root))
    output_dir = _validate_local_output_reference(Path(output_dir))
    if not re.fullmatch(r"visual-4fps-global-v1(?:-r[1-9][0-9]*)?", visual_root.name):
        raise ValueError("contact sheets require a dedicated global-v1 source tree")
    if not re.fullmatch(
        r"contact-sheets-global-v1(?:-r[1-9][0-9]*)?", output_dir.name
    ):
        raise ValueError("global contact-sheet output must use its dedicated tree name")
    if output_dir.exists():
        raise FileExistsError(output_dir)
    if not (64 <= tile_width <= 2048 and 64 <= tile_height <= 2048):
        raise ValueError("contact-sheet tile dimensions must be between 64 and 2048")

    contract_path = visual_root / "contract.json"
    ledger_path = visual_root / "target-ledger.jsonl"
    completion_path = visual_root / "completion.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("completed global visual contract is unavailable") from None
    ledger_sha256 = _sha256(ledger_path)
    contract_sha256 = _sha256(contract_path)
    completion_sha256 = _sha256(completion_path)
    if (
        contract.get("pipeline_version") != PIPELINE_VERSION
        or completion.get("pipeline_version") != PIPELINE_VERSION
        or contract.get("global_lattice", {}).get("target_ledger_sha256")
        != ledger_sha256
        or completion.get("target_ledger_sha256") != ledger_sha256
        or completion.get("contract_sha256") != contract_sha256
        or not completion.get("zero_gap_target_indices")
        or not completion.get("zero_duplicate_target_indices")
        or not completion.get("zero_duplicate_source_pts")
        or not completion.get("strictly_monotonic_source_pts")
    ):
        raise RuntimeError("completed global visual integrity contract failed")

    ledger_rows = _load_ledger_rows(ledger_path)
    target_count = int(contract.get("global_lattice", {}).get("target_count", -1))
    if (
        target_count <= 0
        or len(ledger_rows) != target_count
        or [int(row["target_index"]) for row in ledger_rows]
        != list(range(target_count))
        or int(completion.get("global_target_count", -1)) != target_count
        or int(completion.get("extracted_frame_count", -1)) != target_count
    ):
        raise RuntimeError("completed global visual target count failed")

    source_sha256 = str(contract.get("source", {}).get("sha256", ""))
    section_rows = contract.get("sections", [])
    expected_sections = list(range(len(section_rows)))
    completion_chunks = completion.get("chunks")
    if (
        not re.fullmatch(r"[0-9A-F]{64}", source_sha256)
        or [int(row.get("index", -1)) for row in section_rows] != expected_sections
        or not isinstance(completion_chunks, list)
        or [int(row.get("section_index", -1)) for row in completion_chunks]
        != expected_sections
    ):
        raise RuntimeError("completed global visual section set failed")

    validated_frames: list[dict[str, object]] = []
    chunks_root = visual_root / "chunks"
    expected_chunk_names = {f"chunk-{index:05d}" for index in expected_sections}
    actual_chunk_names = {row.name for row in chunks_root.iterdir() if row.is_dir()}
    if actual_chunk_names != expected_chunk_names:
        raise RuntimeError("completed global visual chunk directory set failed")
    for section_index, completion_chunk in zip(expected_sections, completion_chunks):
        expected_rows = [
            row
            for row in ledger_rows
            if int(row["owner_section_index"]) == section_index
        ]
        chunk_dir = chunks_root / f"chunk-{section_index:05d}"
        manifest_path = chunk_dir / "manifest.json"
        if (
            completion_chunk.get("relative_path")
            != manifest_path.relative_to(visual_root).as_posix()
            or completion_chunk.get("sha256") != _sha256(manifest_path)
        ):
            raise RuntimeError("completed global visual chunk receipt failed")
        chunk_manifest = _validate_published_section(
            chunk_dir,
            source_sha256=source_sha256,
            expected_rows=expected_rows,
            section_index=section_index,
        )
        if (
            completion_chunk.get("frames_tree_sha256")
            != chunk_manifest.get("frames_tree_sha256")
            or int(completion_chunk.get("frame_count", -1))
            != len(chunk_manifest["frames"])
        ):
            raise RuntimeError("completed global visual chunk tree failed")
        for frame in chunk_manifest["frames"]:
            copied = dict(frame)
            copied["chunk_relative_path"] = chunk_dir.relative_to(visual_root).as_posix()
            copied["source_frame_path"] = chunk_dir / frame["relative_path"]
            validated_frames.append(copied)

    indices = [int(row["global_target_index"]) for row in validated_frames]
    source_pts = [Fraction(str(row["source_pts_fraction"])) for row in validated_frames]
    if (
        len(validated_frames) != target_count
        or indices != list(range(target_count))
        or len(indices) != len(set(indices))
        or source_pts != sorted(source_pts)
        or len(source_pts) != len(set(source_pts))
    ):
        raise RuntimeError("global contact-sheet frame order failed")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent)
    )
    sheets_dir = staging / "sheets"
    sheets_dir.mkdir()
    try:
        index_records: list[dict[str, object]] = []
        sheet_records_for_tree: list[dict[str, str]] = []
        frames_per_full_sheet = 20
        for sheet_index, offset in enumerate(range(0, target_count, frames_per_full_sheet)):
            group = validated_frames[offset : offset + frames_per_full_sheet]
            if len(group) == frames_per_full_sheet:
                columns, rows = 5, 4
            else:
                # A single exact tail row avoids blank, padded, or duplicated cells.
                columns, rows = len(group), 1
            canvas = Image.new(
                "RGB",
                (columns * tile_width, rows * tile_height),
                "black",
            )
            indexed_frames: list[dict[str, object]] = []
            for cell_index, frame in enumerate(group):
                target_index = int(frame["global_target_index"])
                label = (
                    f"n={target_index} t={frame['global_target_fraction']}s "
                    f"pts={frame['source_pts_fraction']}s"
                )
                tile = _contact_tile(
                    image_path=Path(frame["source_frame_path"]),
                    tile_width=tile_width,
                    tile_height=tile_height,
                    label=label,
                )
                cell_column = cell_index % columns
                cell_row = cell_index // columns
                canvas.paste(tile, (cell_column * tile_width, cell_row * tile_height))
                indexed_frames.append(
                    {
                        "cell_index": cell_index,
                        "cell_column": cell_column,
                        "cell_row": cell_row,
                        "global_target_index": target_index,
                        "global_target_fraction": frame["global_target_fraction"],
                        "sample_target_seconds": frame["sample_target_seconds"],
                        "source_pts_fraction": frame["source_pts_fraction"],
                        "source_pts_seconds": frame["source_pts_seconds"],
                        "owner_section_index": frame["owner_section_index"],
                        "source_chunk_relative_path": frame["chunk_relative_path"],
                        "source_frame_relative_path": frame["relative_path"],
                        "source_frame_sha256": frame["sha256"],
                    }
                )
            relative_path = f"sheets/sheet_{sheet_index:05d}.jpg"
            sheet_path = staging / relative_path
            canvas.save(
                sheet_path,
                format="JPEG",
                quality=90,
                optimize=True,
                progressive=False,
            )
            sheet_sha256 = _sha256(sheet_path)
            index_records.append(
                {
                    "sheet_index": sheet_index,
                    "relative_path": relative_path,
                    "sha256": sheet_sha256,
                    "width": canvas.width,
                    "height": canvas.height,
                    "columns": columns,
                    "rows": rows,
                    "frame_count": len(group),
                    "padding_cells": 0,
                    "first_global_target_index": int(group[0]["global_target_index"]),
                    "last_global_target_index": int(group[-1]["global_target_index"]),
                    "frames": indexed_frames,
                }
            )
            sheet_records_for_tree.append(
                {"relative_path": relative_path, "sha256": sheet_sha256}
            )

        index_path = staging / "contact-sheet-index.jsonl"
        index_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in index_records),
            encoding="utf-8",
            newline="\n",
        )
        sheet_tree_material = "\n".join(
            f"{row['relative_path']}\0{row['sha256']}" for row in sheet_records_for_tree
        ).encode("utf-8")
        manifest = {
            "schema_version": 1,
            "pipeline_version": "global-contact-sheets-v1",
            "source_tree": "visual-4fps-global-v1-only",
            "source_tree_directory": visual_root.name,
            "view_state": "generated_unviewed",
            "visual_root": str(visual_root.resolve()),
            "source_sha256": source_sha256,
            "research_contract_sha256": contract["settings"][
                "research_contract_sha256"
            ],
            "visual_contract_sha256": contract_sha256,
            "target_ledger_sha256": ledger_sha256,
            "visual_completion_sha256": completion_sha256,
            "total_frames": target_count,
            "total_sheets": len(index_records),
            "frames_per_full_sheet": frames_per_full_sheet,
            "full_sheet_layout": {"columns": 5, "rows": 4},
            "tail_layout_policy": "single_exact_row_no_blank_or_duplicate_cells",
            "padding_cells_total": 0,
            "tile_width": tile_width,
            "tile_height": tile_height,
            "contact_sheet_index_sha256": _sha256(index_path),
            "sheets_tree_sha256": hashlib.sha256(sheet_tree_material).hexdigest().upper(),
            "generator_script_sha256": _sha256(Path(__file__).resolve()),
            "visual_review_claimed": False,
        }
        manifest_path = staging / "contact-sheet-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        expected_files = {
            "contact-sheet-manifest.json",
            "contact-sheet-index.jsonl",
        } | {row["relative_path"] for row in index_records}
        actual_files = {
            candidate.relative_to(staging).as_posix()
            for candidate in staging.rglob("*")
            if candidate.is_file()
        }
        if actual_files != expected_files:
            raise RuntimeError("staged global contact-sheet file set mismatch")
        if [
            frame["global_target_index"]
            for record in index_records
            for frame in record["frames"]
        ] != list(range(target_count)):
            raise RuntimeError("global contact-sheet target index coverage failed")
        for record in index_records:
            if _sha256(staging / record["relative_path"]) != record["sha256"]:
                raise RuntimeError("staged global contact-sheet hash mismatch")
        if output_dir.exists():
            raise FileExistsError(output_dir)
        os.rename(staging, output_dir)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
