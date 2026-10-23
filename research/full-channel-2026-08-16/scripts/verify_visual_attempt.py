#!/usr/bin/env python3
"""Read-only deep verification for one completed visual-review attempt."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def leaf_tree(rows: list[tuple[str, str]]) -> str:
    payload = "".join(
        f"{digest.upper()}  {relative.replace(chr(92), '/')}\n"
        for digest, relative in sorted(rows, key=lambda item: item[1].replace(chr(92), "/"))
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def frame_tree(frames: list[dict]) -> str:
    material = "\n".join(
        f"{row['relative_path']}\0{row['sha256']}\0{float(row['source_pts_seconds']):.9f}"
        for row in frames
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def sheet_tree(rows: list[dict]) -> str:
    material = "\n".join(
        f"{row['relative_path']}\0{row['sha256']}" for row in rows
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def verify_frames(attempt: Path, errors: list[str]) -> tuple[int, list[dict]]:
    manifests: list[tuple[Path, dict]] = []
    for path in attempt.rglob("manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("frames"), list):
            manifests.append((path, data))
    if not manifests:
        fail(errors, "no visual frame manifests found")
        return 0, []
    manifests.sort(key=lambda pair: float(pair[1].get("settings", {}).get("start_seconds", 0)))

    total = 0
    global_rows: list[dict] = []
    source_hashes: set[str] = set()
    for manifest_path, manifest in manifests:
        frames = manifest["frames"]
        parent = manifest_path.parent
        declared_paths: set[str] = set()
        settings = manifest.get("settings", {})
        start = float(settings.get("start_seconds", 0.0))
        if manifest.get("pipeline_version") != "visual-chunk-v5":
            fail(errors, f"{manifest_path}: pipeline_version is not visual-chunk-v5")
        if settings.get("baseline_fps") != 4 or settings.get("long_edge") != 448:
            fail(errors, f"{manifest_path}: requested 4fps/448 settings missing")
        if settings.get("interpolation_allowed") is not False:
            fail(errors, f"{manifest_path}: interpolation must be false")
        source = manifest.get("source", {})
        if source.get("hash_stable_during_extraction") is not True:
            fail(errors, f"{manifest_path}: source hash was not stable")
        if source.get("sha256"):
            source_hashes.add(str(source["sha256"]).upper())
        observed_rate = float(
            source.get("avg_fps") or source.get("nominal_fps") or 0.0
        )
        max_sampling_lag = (
            max(1.25 / observed_rate, 0.001) if observed_rate > 0 else 0.001
        )
        for index, row in enumerate(frames):
            rel = str(row.get("relative_path", "")).replace("\\", "/")
            declared_paths.add(rel)
            path = parent / Path(rel)
            if row.get("sequence") != index:
                fail(errors, f"{manifest_path}: frame sequence {index} mismatch")
            target = float(row.get("sample_target_seconds", -1))
            expected_target = start + index / 4.0
            if abs(target - expected_target) > 1e-6:
                fail(errors, f"{manifest_path}: frame {index} target cadence mismatch")
            pts = float(row.get("source_pts_seconds", -1))
            sampling_error = pts - expected_target
            if sampling_error < -1e-6 or sampling_error > max_sampling_lag + 1e-9:
                fail(errors, f"{manifest_path}: frame {index} source PTS mismatch")
            if int(row.get("width", 0)) > 448 or int(row.get("height", 0)) > 448:
                fail(errors, f"{manifest_path}: frame {index} exceeds long edge")
            if not path.is_file():
                fail(errors, f"missing frame: {path}")
                continue
            actual = sha256(path)
            declared = str(row.get("sha256", "")).upper()
            if actual != declared:
                fail(errors, f"frame hash mismatch: {path}")
            global_rows.append(
                {
                    "sha256": actual,
                    "source_pts_seconds": pts,
                    "sample_target_seconds": target,
                    "path": path,
                }
            )
        actual_paths = {
            path.relative_to(parent).as_posix()
            for path in (parent / "frames").glob("*.jpg")
            if path.is_file()
        }
        if actual_paths != declared_paths:
            fail(errors, f"{manifest_path}: declared/actual frame set mismatch")
        if manifest.get("frames_tree_sha256", "").upper() != frame_tree(frames):
            fail(errors, f"{manifest_path}: frame tree mismatch")
        total += len(frames)
    if len(source_hashes) != 1:
        fail(errors, f"visual manifests have {len(source_hashes)} distinct source hashes")
    for index, row in enumerate(global_rows):
        if abs(row["sample_target_seconds"] - index / 4.0) > 1e-6:
            fail(errors, f"global frame {index}: target sequence mismatch")
    return total, global_rows


def verify_jsonl_sheets(attempt: Path, index_path: Path, frame_rows: list[dict], errors: list[str]) -> int:
    rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    declared: set[str] = set()
    tree_rows: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if row.get("sheet_index") != index:
            fail(errors, f"{index_path}: sheet sequence {index} mismatch")
        rel = str(row["relative_path"]).replace("\\", "/")
        declared.add(rel)
        path = index_path.parent / Path(rel)
        if not path.is_file():
            fail(errors, f"missing sheet: {path}")
            continue
        actual = sha256(path)
        if actual != str(row.get("sha256", "")).upper():
            fail(errors, f"sheet hash mismatch: {path}")
        tree_rows.append((actual, rel))
        frames = row.get("frames", [])
        expected_count = min(20, max(0, len(frame_rows) - index * 20))
        if row.get("frame_count") != expected_count or len(frames) != expected_count:
            fail(errors, f"{index_path}: sheet {index} frame count mismatch")
        for local_index, ref in enumerate(frames):
            global_index = index * 20 + local_index
            if ref.get("global_sequence") != global_index:
                fail(errors, f"{index_path}: sheet {index} global sequence mismatch")
            if global_index < len(frame_rows):
                source = frame_rows[global_index]
                if str(ref.get("source_frame_sha256", "")).upper() != source["sha256"]:
                    fail(errors, f"{index_path}: sheet {index} source hash mismatch")
                if abs(float(ref.get("sample_target_seconds", -1)) - global_index / 4.0) > 1e-6:
                    fail(errors, f"{index_path}: sheet {index} target mismatch")
    actual = {
        path.relative_to(index_path.parent).as_posix()
        for path in (index_path.parent / "sheets").glob("*.jpg")
        if path.is_file()
    }
    if actual != declared:
        fail(errors, f"{index_path}: declared/actual sheet set mismatch")
    manifest_path = index_path.parent / "contact-sheet-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("index_sha256", "").upper() != sha256(index_path):
            fail(errors, f"{manifest_path}: index hash mismatch")
        if manifest.get("sheets_tree_sha256", "").upper() != sheet_tree(rows):
            fail(errors, f"{manifest_path}: sheet tree mismatch")
        if manifest.get("total_frames") != len(frame_rows) or manifest.get("total_sheets") != len(rows):
            fail(errors, f"{manifest_path}: summary count mismatch")
    return len(rows)


def verify_csv_sheets(index_path: Path, frame_count: int, errors: list[str]) -> int:
    with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    declared: set[Path] = set()
    current_chunk = None
    local_sheet_index = 0
    for index, row in enumerate(rows):
        if int(row["sequence"]) != index:
            fail(errors, f"{index_path}: sheet sequence {index} mismatch")
        path = index_path.parent / row["chunk"] / row["sheet"]
        declared.add(path.resolve())
        if not path.is_file():
            fail(errors, f"missing sheet: {path}")
            continue
        if sha256(path) != row["sha256"].upper():
            fail(errors, f"sheet hash mismatch: {path}")
        if row["chunk"] != current_chunk:
            current_chunk = row["chunk"]
            local_sheet_index = 0
        expected_start = local_sheet_index * 20
        expected_count = min(20, frame_count - index * 20)
        expected_end = expected_start + expected_count - 1
        if int(row["source_frame_start"]) != expected_start or int(row["source_frame_end"]) != expected_end:
            fail(errors, f"{index_path}: sheet {index} frame range mismatch")
        start_seconds = row.get("start_seconds")
        if start_seconds is None:
            start_seconds = row.get("requested_target_start")
        if start_seconds is None:
            fail(errors, f"{index_path}: sheet {index} has no target-start field")
        elif abs(float(start_seconds) - index * 5.0) > 1e-6:
            fail(errors, f"{index_path}: sheet {index} start mismatch")
        local_sheet_index += 1
    actual = {path.resolve() for path in index_path.parent.rglob("sheet_*.jpg") if path.is_file()}
    if actual != declared:
        fail(errors, f"{index_path}: declared/actual sheet set mismatch")
    expected_sheets = (frame_count + 19) // 20
    if len(rows) != expected_sheets:
        fail(errors, f"{index_path}: expected {expected_sheets} sheets, got {len(rows)}")
    return len(rows)


def attempt_tree(attempt: Path) -> tuple[int, int, str]:
    paths = sorted((path for path in attempt.rglob("*") if path.is_file()), key=lambda p: p.relative_to(attempt).as_posix())
    rows = [(sha256(path), path.relative_to(attempt).as_posix()) for path in paths]
    return len(paths), sum(path.stat().st_size for path in paths), leaf_tree(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    attempt = args.attempt.resolve()
    errors: list[str] = []
    if not attempt.is_dir():
        raise SystemExit(f"not a directory: {attempt}")
    frame_count, frame_rows = verify_frames(attempt, errors)
    jsonl_indices = list(attempt.rglob("contact-sheet-index.jsonl"))
    csv_indices = [path for path in attempt.rglob("index.csv") if "sheet" in path.parent.name.lower()]
    if jsonl_indices:
        sheet_count = verify_jsonl_sheets(attempt, jsonl_indices[0], frame_rows, errors)
    elif csv_indices:
        sheet_count = verify_csv_sheets(csv_indices[0], frame_count, errors)
    else:
        sheet_count = 0
        fail(errors, "no contact-sheet index found")
    vtt_count = sum(1 for path in attempt.rglob("*.vtt") if path.is_file())
    if vtt_count != 1:
        fail(errors, f"expected exactly one VTT, found {vtt_count}")
    files, bytes_total, tree = attempt_tree(attempt)
    result = {
        "attempt": str(attempt),
        "frames": frame_count,
        "sheets": sheet_count,
        "vtt_files": vtt_count,
        "files": files,
        "bytes": bytes_total,
        "attempt_tree_sha256": tree,
        "errors": errors,
        "ok": not errors,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
