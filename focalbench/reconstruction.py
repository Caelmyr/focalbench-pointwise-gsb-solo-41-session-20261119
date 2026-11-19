"""Reconstruct an immutable image result from one reviewed stack revision."""

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .alignment import AnalysisBox, RailAlignment, output_viewport, overlap_summary
from .focus import FocusEvidence, analyze_peaks, frame_evidence, image_response, rail_statistics
from .fusion import ProductImages, assemble_composite, output_images
from .imaging import assert_compatible_dimensions, linear_luminance, processing_size, read_pixels
from .models import RUN_ASSETS, Capacity, Conflict, RunSettings, Unprocessable, require_frame_count, storage_id
from .surface import focus_surface_summary

ALGORITHM_VERSION = "1.0"
RESULT_LIMIT = 36
RESULT_LAYERS = (
    "depth", "confidence", "selected", "alternate", "valid", "reliable", "interpolated", "contributors",
    "response", "response-support",
)


@dataclass
class StackPlanes:
    rgb: np.ndarray
    coverage: np.ndarray
    response: np.ndarray
    response_support: np.ndarray
    motions: list[dict]

    def cut(self, box: AnalysisBox) -> None:
        vertical = slice(box.y, box.y + box.height)
        horizontal = slice(box.x, box.x + box.width)
        self.rgb = self.rgb[:, vertical, horizontal]
        self.coverage = self.coverage[:, vertical, horizontal]
        self.response = self.response[:, vertical, horizontal]
        self.response_support = self.response_support[:, vertical, horizontal]


@dataclass
class Calculation:
    evidence: FocusEvidence
    planes: StackPlanes
    products: ProductImages
    displayed_depth: np.ndarray
    summary: dict


def selected_captures(snapshot: dict, settings: RunSettings):
    captures = sorted(snapshot["captures"], key=lambda capture: (capture["position"], capture["id"]))
    require_frame_count(len(captures))
    assert_compatible_dimensions(captures)
    positions = np.asarray([capture["position"] for capture in captures], dtype=np.float64)
    if np.any(np.diff(positions) < 1e-8):
        raise Unprocessable("Use distinct focus positions for each enabled capture.", field="position")
    if settings.reference_id is None:
        reference = len(captures) // 2
    else:
        choices = [i for i, capture in enumerate(captures) if capture["id"] == settings.reference_id]
        if not choices:
            raise Unprocessable("Include the selected reference capture.", field="reference_id")
        reference = choices[0]
    return captures, positions, reference


def capture_stack(library, captures: list[dict], reference: int, settings: RunSettings, size: tuple[int, int]) -> StackPlanes:
    channels = len(captures)
    width, height = size
    rgb = np.empty((channels, height, width, 3), dtype=np.float32)
    coverage = np.empty((channels, height, width), dtype=bool)
    response = np.empty((channels, height, width), dtype=np.float32)
    response_support = np.empty((channels, height, width), dtype=bool)
    motion_rows = []
    reference_capture = read_pixels(library, captures[reference], size)
    if not reference_capture.support.any():
        raise Unprocessable("The selected reference has no opaque samples.")
    aligner = RailAlignment(reference_capture, settings.registration)
    for index, capture in enumerate(captures):
        pixels = reference_capture if index == reference else read_pixels(library, capture, size)
        if not pixels.support.any():
            raise Unprocessable(f"{capture['name']} has no opaque samples. Exclude it before reconstructing.")
        aligned = aligner.align(pixels, reference=index == reference)
        rgb[index] = aligned.rgb
        coverage[index] = aligned.support
        focus, eligible = image_response(
            linear_luminance(aligned.rgb), aligned.support, settings.score, settings.edge_guard,
        )
        response[index] = focus
        response_support[index] = eligible
        motion_rows.append({
            **aligned.motion.public(),
            "source_coverage": round(float(pixels.support.mean()), 6),
            "aligned_coverage": round(float(aligned.support.mean()), 6),
        })
    return StackPlanes(rgb, coverage, response, response_support, motion_rows)


def surface_coordinates(positions: np.ndarray, scale) -> tuple[np.ndarray, np.ndarray]:
    """Plane coordinates for the depth fit and the displayed surface values.

    An uncalibrated rail is linear, so both are the same array. A calibrated
    rail keeps stage steps for the local fit and uses the measured height table
    for the reported value; planes outside the marks are NaN and thus unmeasured.
    """
    if not scale.calibrated:
        return positions, np.asarray(
            [scale.display_position(float(position)) for position in positions],
            dtype=np.float64,
        )
    display = np.asarray(
        [np.nan if height is None else height for height in
         (scale.calibration.height_at(position) for position in positions)],
        dtype=np.float64,
    )
    return positions, display


def source_rows(positions, captures, settings, evidence_rows, motions, reference, display=None):
    if display is None:
        display = np.asarray([settings.scale.display_position(position) for position in positions])
    rows = []
    for index, capture in enumerate(captures):
        position = float(positions[index])
        shown = display[index]
        row = {
            "index": index,
            "id": capture["id"],
            "slot": capture["slot"],
            "name": capture["name"],
            "position": position,
            "display_position": None if not np.isfinite(shown) else float(shown),
            "inside_calibration": bool(np.isfinite(shown)),
            "is_reference": index == reference,
            "motion": motions[index],
            **evidence_rows[index],
        }
        rows.append(row)
    return rows


def run_warnings(coverage: dict, surface: dict, captures: list[dict], statistics: dict) -> list[dict]:
    warnings = []
    if coverage["visible_fraction"] < 0.98:
        warnings.append({"code": "holes", "detail": "Some output pixels lack any aligned capture."})
    if coverage["shared_fraction"] < 0.75:
        warnings.append({"code": "overlap", "detail": "Less than 75% of the output is covered by all captures."})
    if surface["reliable_fraction"] < 0.42:
        warnings.append({"code": "low-confidence", "detail": "Much of the surface lacks discriminating focus evidence."})
    if not statistics["uniform"]:
        warnings.append({"code": "nonuniform-positions", "detail": "This stack uses unequal rail steps."})
    beyond = [capture["name"] for capture in captures if not capture.get("inside_calibration", True)]
    if beyond:
        names = ", ".join(beyond[:4]) + (" …" if len(beyond) > 4 else "")
        warnings.append({
            "code": "outside-rail-calibration",
            "detail": (
                f"{len(beyond)} capture(s) sit outside the measured rail pairs "
                f"({names}). They still compose the image, but their surface depth is left empty."
            ),
        })
    for capture in captures:
        warning = capture["motion"]["warning"]
        if warning:
            warnings.append({"code": "alignment", "capture_id": capture["id"],
                             "detail": f"{capture['name']}: {warning}"})
    return warnings


def summarize_result(
    snapshot: dict,
    box: AnalysisBox,
    size: tuple[int, int],
    settings: RunSettings,
    captures: list[dict],
    reference: int,
    evidence: FocusEvidence,
    planes: StackPlanes,
    positions: np.ndarray,
    displayed_depth: np.ndarray,
    display: np.ndarray,
) -> dict:
    surfaces = focus_surface_summary(evidence, displayed_depth)
    coverage = overlap_summary(planes.coverage, AnalysisBox(0, 0, box.width, box.height))
    frames = frame_evidence(planes.response, planes.response_support, evidence)
    rows = source_rows(positions, captures, settings, frames, planes.motions, reference, display)
    stats = rail_statistics(positions)
    origin = captures[reference]
    measured = np.isfinite(display)
    scale = settings.scale
    if scale.calibrated:
        calibrated = {
            "mode": "calibration",
            "unit": "µm",
            "inside_range": measured.tolist(),
            "points": scale.calibration.public()["points"],
        }
    else:
        calibrated = None
    return {
        "algorithm": ALGORITHM_VERSION,
        "project_name": snapshot["project_name"],
        "source_revision": snapshot["revision"],
        "width": box.width,
        "height": box.height,
        "original_size": [origin["width"], origin["height"]],
        "processed_size": list(size),
        "crop": box.public(),
        "settings": settings.public(),
        "scale": settings.public()["scale"],
        "surface_unit": scale.surface_unit,
        "rail_calibration": calibrated,
        "capture_count": len(captures),
        "reference_id": origin["id"],
        "reference_name": origin["name"],
        "captures": rows,
        "coverage": coverage,
        "rail": stats,
        "depth_encoding": {
            "format": "png-uint16",
            "no_data": 0,
            "minimum_code": 1,
            "maximum_code": 65535,
            "near": float(np.nanmin(display)) if measured.any() else None,
            "far": float(np.nanmax(display)) if measured.any() else None,
            "unit": scale.surface_unit,
            "calibrated": scale.calibrated,
            "unmeasured_outside_calibration": bool(np.any(~measured)),
        },
        "warnings": run_warnings(coverage, surfaces, rows, stats),
        **surfaces,
    }


def archive_estimate(size: tuple[int, int], planes: int) -> dict:
    pixels = size[0] * size[1]
    stack_bytes = pixels * planes * (
        np.dtype(np.float32).itemsize + np.dtype(np.bool_).itemsize
    )
    map_bytes = pixels * (
        np.dtype(np.float64).itemsize + np.dtype(np.float32).itemsize
        + 3 * np.dtype(np.bool_).itemsize + 3 * np.dtype(np.uint8).itemsize
    )
    image_bytes = pixels * (2 * 4 + 4 * 3 + 2)
    return {
        "maximum_bytes": int(stack_bytes + map_bytes + image_bytes + 512 * 1024),
        "response_bytes": int(stack_bytes),
        "processed_size": list(size),
    }


def assess_storage(library, size: tuple[int, int], planes: int) -> dict:
    estimate = archive_estimate(size, planes)
    available = shutil.disk_usage(library.products).free
    estimate["available_bytes"] = available
    if available < estimate["maximum_bytes"]:
        raise Capacity(
            "There is too little free space for this reconstruction and its measurement layers.",
            details=estimate,
        )
    return estimate


def calculate(library, snapshot: dict) -> Calculation:
    settings = RunSettings.parse(snapshot["settings"])
    captures, positions, reference = selected_captures(snapshot, settings)
    size = processing_size(captures)
    assess_storage(library, size, len(captures))
    planes = capture_stack(library, captures, reference, settings, size)
    box = output_viewport(planes.coverage, settings.viewport)
    planes.cut(box)
    _, display = surface_coordinates(positions, settings.scale)
    peaks = analyze_peaks(
        planes.response, planes.coverage, planes.response_support, positions,
        settings.confidence_floor, display,
    )
    color = assemble_composite(planes.rgb, planes.coverage, planes.response, peaks, settings.blend, settings.strength)
    measured = display[np.isfinite(display)]
    products = output_images(
        color, peaks, planes.coverage,
        (float(measured.min()), float(measured.max())) if measured.size else (0.0, 0.0),
    )
    displayed_depth = peaks.depth.astype(np.float64, copy=True)
    summary = summarize_result(
        snapshot, box, size, settings, captures, reference, peaks, planes, positions,
        displayed_depth, display,
    )
    return Calculation(peaks, planes, products, displayed_depth, summary)


def _save_image(path: Path, image, *, format: str = "PNG", **options) -> None:
    with path.open("wb") as output:
        image.save(output, format=format, **options)
        output.flush()
        os.fsync(output.fileno())


def _save_array(path: Path, array: np.ndarray) -> None:
    with path.open("wb") as output:
        np.save(output, array, allow_pickle=False)
        output.flush()
        os.fsync(output.fileno())


def store_calculation(path: Path, calculation: Calculation):
    products = calculation.products
    for key, basename in RUN_ASSETS.items():
        if key == "overview":
            picture = products.composite.copy()
            picture.thumbnail((450, 340))
            _save_image(path / basename, picture, format="WEBP", quality=85)
        else:
            _save_image(path / basename, getattr(products, key), compress_level=4)
    archive = {
        "depth": calculation.displayed_depth,
        "confidence": calculation.evidence.confidence,
        "selected": calculation.evidence.selected.astype(np.uint8),
        "alternate": calculation.evidence.alternate.astype(np.uint8),
        "valid": calculation.evidence.valid,
        "reliable": calculation.evidence.reliable,
        "interpolated": calculation.evidence.interpolated,
        "contributors": calculation.planes.coverage.sum(axis=0, dtype=np.uint8),
        "response": calculation.planes.response,
        "response-support": calculation.planes.response_support,
    }
    for layer in RESULT_LAYERS:
        _save_array(path / (layer + ".npy"), archive[layer])


class Reconstruction:
    def __init__(self, library):
        self.library = library
        self.compute_slot = threading.Lock()

    def build(self, project_id: str, title: str, revision: int | None):
        if not self.compute_slot.acquire(blocking=False):
            raise Conflict("The bench is reconstructing another stack. Wait for it to finish.")
        try:
            return self._build(project_id, title, revision)
        finally:
            self.compute_slot.release()

    def _build(self, project_id: str, title: str, revision: int | None):
        project = self.library.view_project(project_id)
        if len(project["results"]) >= RESULT_LIMIT:
            raise Conflict(f"This stack has {RESULT_LIMIT} archived reconstructions.")
        snapshot = self.library.snapshot(project_id, revision)
        calculation = calculate(self.library, snapshot)
        run_id = storage_id()
        final = self.library.run_directory(run_id)
        with tempfile.TemporaryDirectory(prefix=".reconstruct-", dir=self.library.products) as parent:
            working = Path(parent) / "assets"
            working.mkdir()
            store_calculation(working, calculation)
            os.replace(working, final)
            try:
                return self.library.publish_run(project_id, run_id, title, snapshot, calculation.summary)
            except Exception:
                shutil.rmtree(final)
                raise
