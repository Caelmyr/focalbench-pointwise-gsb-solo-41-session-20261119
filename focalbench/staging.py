"""Attach a measured rail log to matching captures, with an explicit preview."""

import csv
import io
from collections import Counter, defaultdict

from .models import Capacity, CaptureScale, Unprocessable, number

MAX_LOG_BYTES = 100_000
MAX_LINES = 256
UNIT_SCALE = {"mm": 1000.0, "µm": 1.0, "step": None}
ALIASES = {"um": "µm", "μm": "µm", "micrometers": "µm", "steps": "step"}


def _parse_unit(value: str, fallback: str) -> str:
    normalized = value.strip().casefold()
    unit = ALIASES.get(normalized, normalized) or fallback
    if unit not in UNIT_SCALE:
        raise Unprocessable(f"Unknown rail unit {unit!r} in the log.")
    return unit


def _to_project(value: float, source_unit: str, target_unit: str, calibration=None) -> float:
    if source_unit == target_unit:
        return value
    first = UNIT_SCALE[source_unit]
    last = UNIT_SCALE[target_unit]
    if first is None or last is None:
        # A calibrated rail is the only way to turn a measured height back
        # into the stage step that produced it.
        if calibration is not None and target_unit == "step" and source_unit == "µm":
            step = calibration.step_at(value)
            if step is None:
                raise Unprocessable(
                    f"{value:g} µm is outside the measured rail calibration.",
                )
            return number(step, "converted position", -1e7, 1e7)
        raise Unprocessable("A step count cannot be converted to a physical position without calibration.")
    return number(value * first / last, "converted position", -1e7, 1e7)


def _read_log(raw: bytes, target_unit: str, calibration=None) -> list[dict]:
    if len(raw) > MAX_LOG_BYTES:
        raise Capacity("A rail log may contain up to 100 kB.")
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise Unprocessable("Use an UTF-8 rail log.") from error
    table = csv.DictReader(io.StringIO(source, newline=""))
    if not table.fieldnames:
        raise Unprocessable("Add a filename,position header to the rail log.")
    table.fieldnames = [field.strip().casefold() for field in table.fieldnames]
    if len(set(table.fieldnames)) != len(table.fieldnames):
        raise Unprocessable("Rail log column names are repeated.")
    if not {"filename", "position"} <= set(table.fieldnames):
        raise Unprocessable("The rail log needs filename and position columns.")
    results = []
    try:
        for row in table:
            if table.line_num > MAX_LINES:
                raise Capacity("The rail log may contain at most 255 data lines.")
            if None in row or any(value is None for value in row.values()):
                raise Unprocessable(f"Rail log line {table.line_num} has the wrong column count.")
            source_unit = _parse_unit(row.get("unit", ""), target_unit)
            file_name = row["filename"].strip()
            if not file_name or len(file_name) > 220 or any(char in file_name for char in ("/", "\\")):
                raise Unprocessable(f"Rail log line {table.line_num} must name a capture with no folder.")
            original = number(row["position"], f"position on line {table.line_num}", -1e7, 1e7)
            results.append({
                "line": table.line_num, "filename": file_name, "raw_position": original,
                "raw_unit": source_unit,
                "new_position": _to_project(original, source_unit, target_unit, calibration),
            })
    except csv.Error as error:
        raise Unprocessable("The rail log could not be parsed as CSV.") from error
    if not results:
        raise Unprocessable("The rail log does not contain any captures.")
    return results


def stage_log(project: dict, raw: bytes) -> tuple[dict, dict[str, float]]:
    scale = project["settings"]["scale"]
    unit = scale["rail_unit"]
    calibration = None
    if scale.get("rail_mode") == "calibration" and scale.get("calibration"):
        calibration = CaptureScale.parse(scale).calibration
    records = _read_log(raw, unit, calibration)
    captures = [item for item in project["captures"] if not item["archived"]]
    by_name = defaultdict(list)
    for capture in captures:
        by_name[capture["name"]].append(capture)
    repeated_log = Counter(record["filename"] for record in records)
    planned = {}
    results = []
    for record in records:
        matches = by_name[record["filename"]]
        result = {"status": "matched", "capture_id": None, "old_position": None, **record}
        if repeated_log[record["filename"]] > 1:
            result["status"] = "repeated-in-log"
        elif not matches:
            result["status"] = "unknown-capture"
        elif len(matches) > 1:
            result["status"] = "ambiguous-capture"
        else:
            capture = matches[0]
            result["capture_id"] = capture["id"]
            result["old_position"] = capture["position"]
            planned[capture["id"]] = record["new_position"]
        results.append(result)
    assigned = defaultdict(list)
    for capture in captures:
        if capture["included"]:
            key = planned.get(capture["id"], capture["position"])
            assigned[key].append(capture["name"])
    conflicts = [
        {"position": position, "captures": names}
        for position, names in sorted(assigned.items()) if len(names) > 1
    ]
    status = Counter(row["status"] for row in results)
    output = {
        "stack_revision": project["revision"],
        "rail_unit": unit,
        "rows": results,
        "counts": dict(status),
        "collisions": conflicts,
        "valid": not conflicts and status.get("matched") == len(records),
    }
    return output, planned
