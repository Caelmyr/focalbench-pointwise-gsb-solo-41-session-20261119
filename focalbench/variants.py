"""Compare two processing recipes of the same recorded reference frame."""

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .fusion import covered_image
from .imaging import percentile_summary
from .models import RUN_ASSETS, Unprocessable
from .reports import csv_bytes
from .surface import CoordinateFrame, Inspection

MAX_COMPARISON_EDGE = 1100
IN_SRGB = {
    "positive": np.array((224, 135, 96)),
    "negative": np.array((102, 176, 227)),
    "unchanged": np.array((209, 213, 203)),
}


@dataclass(frozen=True)
class Grid:
    x: np.ndarray
    y: np.ndarray
    width: int
    height: int
    extent: tuple[float, float, float, float]

    def projected(self, frame: CoordinateFrame) -> tuple[np.ndarray, np.ndarray]:
        column = self.x / frame.width_ratio - frame.crop_x - 0.5
        row = self.y / frame.height_ratio - frame.crop_y - 0.5
        return np.meshgrid(row, column, indexing="ij")


@dataclass
class RerenderComparison:
    grid: Grid
    first: Inspection
    second: Inspection
    first_depth: np.ndarray
    second_depth: np.ndarray
    first_valid: np.ndarray
    second_valid: np.ndarray
    first_visible: np.ndarray
    second_visible: np.ndarray
    common: np.ndarray
    difference: np.ndarray
    unit: str


def _extent(frame: CoordinateFrame) -> tuple[float, float, float, float]:
    p0 = frame.reference_point((0, 0))
    p1 = frame.reference_point((1, 1))
    return (*p0, *p1)


def common_grid(first: CoordinateFrame, second: CoordinateFrame) -> Grid:
    lower = _extent(first)
    upper = _extent(second)
    left = max(lower[0], upper[0])
    top = max(lower[1], upper[1])
    right = min(lower[2], upper[2])
    bottom = min(lower[3], upper[3])
    if right - left < 4 or bottom - top < 4:
        raise Unprocessable("These result viewports share too little image area.")
    spacing_x = max(first.width_ratio, second.width_ratio, (right - left) / MAX_COMPARISON_EDGE)
    spacing_y = max(first.height_ratio, second.height_ratio, (bottom - top) / MAX_COMPARISON_EDGE)
    width = int(np.floor((right - left) / spacing_x))
    height = int(np.floor((bottom - top) / spacing_y))
    if min(width, height) < 4:
        raise Unprocessable("These results have too little shared image detail.")
    x = left + (np.arange(width, dtype=np.float64) + 0.5) * (right - left) / width
    y = top + (np.arange(height, dtype=np.float64) + 0.5) * (bottom - top) / height
    return Grid(x, y, width, height, (left, top, right, bottom))


def _original_unit(values: np.ndarray, inspection: Inspection) -> np.ndarray:
    if inspection.summary.get("rail_calibration"):
        return values
    original = values + inspection.summary["scale"]["rail_offset"]
    return original * 1000 if inspection.unit == "mm" else original


def _can_compare(first: Inspection, second: Inspection) -> str:
    if first.summary["reference_id"] != second.summary["reference_id"]:
        raise Unprocessable("Choose two results with the same reference capture.")
    if bool(first.summary.get("rail_calibration")) != bool(second.summary.get("rail_calibration")):
        raise Unprocessable("A calibrated rail result cannot be compared with a result using a linear rail.")
    if (first.frame.original_width, first.frame.original_height) != (
        second.frame.original_width, second.frame.original_height
    ):
        raise Unprocessable("The recorded reference image dimensions differ.")
    steps_one = first.unit == "step"
    steps_two = second.unit == "step"
    if steps_one != steps_two:
        raise Unprocessable("A result in stage steps cannot be compared with a physical rail scale.")
    if not steps_one and first.unit != second.unit:
        first_positions = {row["id"]: row["position"] * (1000 if first.unit == "mm" else 1)
                           for row in first.summary["captures"]}
        second_positions = {row["id"]: row["position"] * (1000 if second.unit == "mm" else 1)
                            for row in second.summary["captures"]}
        common_captures = first_positions.keys() & second_positions.keys()
        if any(not np.isclose(first_positions[key], second_positions[key], atol=1e-6, rtol=1e-5)
               for key in common_captures):
            raise Unprocessable("Shared captures do not have equivalent calibrated rail positions.")
    return "step" if steps_one else "µm"


def _resample(values: np.ndarray, coords, *, order: int = 1) -> np.ndarray:
    return ndimage.map_coordinates(
        values, coords, order=order, mode="nearest", prefilter=False,
    )


def _resample_surface(inspection: Inspection, grid: Grid) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    coordinates = grid.projected(inspection.frame)
    visible = _resample(inspection.valid.astype(np.float32), coordinates) > 0.999
    solid = _resample(inspection.reliable.astype(np.float32), coordinates) > 0.999
    underlying = np.where(inspection.reliable, inspection.depth, 0)
    values = _original_unit(_resample(underlying, coordinates), inspection)
    values[~solid] = np.nan
    return values, solid, visible


def compare_results(first: Inspection, second: Inspection) -> RerenderComparison:
    unit = _can_compare(first, second)
    grid = common_grid(first.frame, second.frame)
    left, left_valid, left_visible = _resample_surface(first, grid)
    right, right_valid, right_visible = _resample_surface(second, grid)
    common = left_valid & right_valid
    difference = np.full(left.shape, np.nan, dtype=np.float64)
    difference[common] = right[common] - left[common]
    return RerenderComparison(
        grid, first, second, left, right, left_valid, right_valid,
        left_visible, right_visible, common, difference, unit,
    )


def _settings_diff(before: dict, after: dict) -> list[dict]:
    keys = sorted(before.keys() | after.keys())
    rows = []
    for field in keys:
        a, b = before.get(field), after.get(field)
        if a == b:
            continue
        if field == "calibration" and isinstance(a, dict) and isinstance(b, dict):
            if a.get("points") == b.get("points"):
                continue
        if isinstance(a, dict) and isinstance(b, dict):
            rows.extend({**item, "field": f"{field}.{item['field']}"} for item in _settings_diff(a, b))
        else:
            rows.append({"field": field, "first": a, "second": b})
    return rows


def _capture_changes(first: Inspection, second: Inspection) -> dict:
    left = {row["id"]: row for row in first.summary["captures"]}
    right = {row["id"]: row for row in second.summary["captures"]}
    left_factor = 1000 if first.unit == "mm" else 1
    right_factor = 1000 if second.unit == "mm" else 1
    changed = []
    for item in sorted(set(left).intersection(right)):
        old, new = left[item]["position"] * left_factor, right[item]["position"] * right_factor
        if not np.isclose(old, new, atol=1e-6, rtol=1e-5):
            changed.append({"id": item, "name": left[item]["name"], "first": old, "second": new})
    return {
        "first_only": [{"id": item, "name": left[item]["name"]} for item in sorted(set(left) - set(right))],
        "second_only": [{"id": item, "name": right[item]["name"]} for item in sorted(set(right) - set(left))],
        "rail_positions_changed": changed,
    }


def describe_comparison(comparison: RerenderComparison) -> dict:
    lookup = comparison
    areas = lookup.common.size
    both = int(lookup.common.sum())
    jointly_visible = int((lookup.first_visible & lookup.second_visible).sum())
    first_only = int((lookup.first_valid & ~lookup.second_valid).sum())
    second_only = int((lookup.second_valid & ~lookup.first_valid).sum())
    changes = _settings_diff(lookup.first.summary["settings"], lookup.second.summary["settings"])
    discrepancies = _capture_changes(lookup.first, lookup.second)
    statistics = percentile_summary(lookup.difference, lookup.common)
    sample = np.abs(lookup.difference[lookup.common])
    return {
        "reference_id": lookup.first.summary["reference_id"],
        "reference_name": lookup.first.summary["reference_name"],
        "grid": [lookup.grid.width, lookup.grid.height],
        "original_edge_bounds": lookup.grid.extent,
        "coverage": {
            "jointly_visible": jointly_visible / areas,
            "jointly_reliable": both / areas,
            "first_only_reliable": first_only / areas,
            "second_only_reliable": second_only / areas,
        },
        "position_unit": lookup.unit,
        "position_difference": statistics,
        "median_absolute_difference": float(np.median(sample)) if sample.size else None,
        "p95_absolute_difference": float(np.percentile(sample, 95)) if sample.size else None,
        "settings_changes": changes,
        "capture_changes": discrepancies,
        "settings_note": (
            "Depth difference compares measured surface heights; a linear rail is "
            "normalized to its full position before subtracting the result origin."
        ),
    }


def _read_composite(inspection: Inspection, grid: Grid) -> np.ndarray:
    with Image.open(inspection.directory / RUN_ASSETS["composite"]) as source:
        rgb = np.asarray(source.convert("RGB"))
    coordinates = grid.projected(inspection.frame)
    values = [_resample(rgb[..., channel], coordinates) for channel in range(3)]
    return np.stack(values, axis=2)


def result_comparison_image(comparison: RerenderComparison, view: str, choice: float) -> Image.Image:
    if view == "split":
        left = _read_composite(comparison.first, comparison.grid)
        right = _read_composite(comparison.second, comparison.grid)
        x = int(np.clip(round(comparison.grid.width * choice), 1, comparison.grid.width - 1))
        left[:, x:] = right[:, x:]
        mask = comparison.first_visible.copy()
        mask[:, x:] = comparison.second_visible[:, x:]
        return covered_image(left, mask)
    if view == "support":
        shape = comparison.common.shape
        colors = np.full((*shape, 3), 27, dtype=np.uint8)
        visible = comparison.first_visible | comparison.second_visible
        colors[visible] = (59, 65, 82)
        colors[comparison.first_valid] = (112, 163, 224)
        colors[comparison.second_valid] = (224, 157, 107)
        colors[comparison.common] = (124, 217, 178)
        return Image.fromarray(colors)
    difference = comparison.difference
    robust = np.abs(difference[comparison.common])
    limit = max(1.0e-7, float(np.percentile(robust, 95))) if robust.size else 1.0
    strength = np.minimum(np.abs(np.nan_to_num(difference, nan=0)) / limit * (0.3 + choice * 1.4), 1)
    positive = difference >= 0
    sign_color = np.where(positive[..., None], IN_SRGB["positive"], IN_SRGB["negative"])
    color = IN_SRGB["unchanged"] * (1 - strength[..., None]) + sign_color * strength[..., None]
    return covered_image(color.astype(np.uint8), comparison.common)


def comparison_report(comparison: RerenderComparison, steps: int) -> bytes:
    grid = comparison.grid
    rows = np.unique(np.linspace(0, grid.height - 1, steps, dtype=np.int32))
    cols = np.unique(np.linspace(0, grid.width - 1, steps, dtype=np.int32))
    records = []
    for row in rows:
        for col in cols:
            first = comparison.first_valid[row, col]
            second = comparison.second_valid[row, col]
            shared = comparison.common[row, col]
            records.append((
                grid.x[col], grid.y[row], comparison.unit,
                comparison.first_depth[row, col] if first else None,
                comparison.second_depth[row, col] if second else None,
                comparison.difference[row, col] if shared else None,
                bool(first), bool(second),
            ))
    return csv_bytes(
        ("reference_x", "reference_y", "unit", "first_position", "second_position",
         "difference", "first_reliable", "second_reliable"),
        records,
    )
