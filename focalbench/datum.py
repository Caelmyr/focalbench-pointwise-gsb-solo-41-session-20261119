"""Choose a zero for later reconstructions from a reliable, archived datum patch."""

import numpy as np

from .models import Conflict, Region, RunSettings, Unprocessable, number


def inspect_datum(inspection, region: Region, proposed: float | None) -> dict:
    measured = inspection.area(region, 12)
    depth = measured["positions"]
    reliable = measured["reliable_fraction"]
    count = measured["reliable_pixels"]
    scale = inspection.summary["scale"]
    saved_origin = scale["rail_offset"]
    unit = inspection.unit
    material = depth["median"]
    if material is None or reliable < 0.35 or count < 12:
        return {
            "ready": False,
            "reason": "Pick a larger, well-focused area to estimate a repeatable datum.",
            "reliable_fraction": reliable,
            "reliable_pixels": count,
            "region": region.public(),
        }
    if inspection.summary.get("rail_calibration"):
        # A measured rail table fixes each step to its micrometre height; a
        # display offset cannot move those readings, so refuse instead of
        # suggesting a datum that would be ignored.
        return {
            "ready": False,
            "reason": "The rail is calibrated with measured step and height pairs, so its readings already have an absolute scale. Clear the rail calibration to use a datum offset.",
            "reliable_fraction": reliable,
            "reliable_pixels": count,
            "region": region.public(),
        }
    wanted = 0.0 if proposed is None else proposed
    next_origin = saved_origin + material - wanted
    limit = max(inspection.known_positions()[-1] - inspection.known_positions()[0], 1e-8)
    spread = depth["p95"] - depth["p05"]
    warning = "The selected patch spans a large range of focal depths." if spread > limit * 0.15 else None
    return {
        "ready": True,
        "warning": warning,
        "reference_id": inspection.summary["reference_id"],
        "region": region.public(),
        "unit": unit,
        "reliable_fraction": reliable,
        "reliable_pixels": count,
        "measured_median": material,
        "measured_p05": depth["p05"],
        "measured_p95": depth["p95"],
        "current_origin": saved_origin,
        "recommended_origin": next_origin,
        "requested_reading": wanted,
        "inside_supported_limits": bool(np.isfinite(next_origin) and abs(next_origin) <= 1e7),
    }


def apply_datum(library, project_id: str, inspection, measurement: dict, expected: int | None):
    if not measurement.get("ready") or not measurement.get("inside_supported_limits"):
        raise Unprocessable("Choose a valid datum patch before changing the rail origin.")
    snapshot = library.snapshot(project_id, expected)
    settings = snapshot["settings"]
    scale = settings["scale"]
    if scale.get("rail_mode") == "calibration":
        raise Conflict("The current rail uses measured step and height pairs. Clear that calibration before applying a datum.")
    if scale["rail_unit"] != measurement["unit"]:
        raise Conflict("The rail unit changed after this result. Reconstruct in current units first.")
    run_captures = {
        capture["id"]: capture["position"] for capture in inspection.summary["captures"]
    }
    source_captures = {
        capture["id"]: capture["position"] for capture in snapshot["captures"]
    }
    if run_captures != source_captures:
        raise Conflict("The current focal coordinates differ from this result. Reconstruct before setting its datum.")
    tested = number(measurement["recommended_origin"], "rail offset", -1e7, 1e7)
    next_settings = {**settings, "scale": {**scale, "rail_offset": tested}}
    return library.update_project(
        project_id, {"settings": RunSettings.parse(next_settings)}, snapshot["revision"],
    )
