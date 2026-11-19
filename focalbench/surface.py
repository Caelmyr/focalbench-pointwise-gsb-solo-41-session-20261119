"""Read and measure an archived focus surface in its frozen coordinate system."""

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .focus import aggregate_response_window, peak_path_from_volume
from .imaging import percentile_summary
from .models import Mark, Missing, Region, Unprocessable


@dataclass(frozen=True)
class CoordinateFrame:
    width: int
    height: int
    crop_x: int
    crop_y: int
    full_width: int
    full_height: int
    original_width: int
    original_height: int
    pixel_um: float | None

    @classmethod
    def from_summary(cls, summary: dict) -> "CoordinateFrame":
        crop = summary["crop"]
        processed = summary["processed_size"]
        original = summary["original_size"]
        return cls(
            summary["width"], summary["height"], crop["x"], crop["y"],
            processed[0], processed[1], original[0], original[1],
            summary["scale"]["pixel_um"],
        )

    @property
    def width_ratio(self) -> float:
        return self.original_width / self.full_width

    @property
    def height_ratio(self) -> float:
        return self.original_height / self.full_height

    def local_pixel(self, point: tuple[float, float]) -> tuple[int, int]:
        return (
            int(np.clip(math.floor(point[0] * self.width), 0, self.width - 1)),
            int(np.clip(math.floor(point[1] * self.height), 0, self.height - 1)),
        )

    def center(self, x: int, y: int) -> tuple[float, float]:
        return (x + 0.5) / self.width, (y + 0.5) / self.height

    def reference_point(self, point: tuple[float, float]) -> tuple[float, float]:
        return (
            (self.crop_x + point[0] * self.width) * self.width_ratio,
            (self.crop_y + point[1] * self.height) * self.height_ratio,
        )

    def distance(self, start: tuple[float, float], end: tuple[float, float]) -> float:
        return math.dist(self.reference_point(start), self.reference_point(end))

    def xy_summary(self, x: int, y: int) -> dict:
        sx, sy = self.reference_point(self.center(x, y))
        physical = (None, None) if self.pixel_um is None else (sx * self.pixel_um, sy * self.pixel_um)
        return {
            "x": x,
            "y": y,
            "source_x": sx,
            "source_y": sy,
            "source_x_um": physical[0],
            "source_y_um": physical[1],
        }

    @property
    def pixel_area_um2(self) -> float | None:
        if self.pixel_um is None:
            return None
        return self.pixel_um ** 2 * self.width_ratio * self.height_ratio


class Inspection:
    def __init__(self, directory: Path, summary: dict):
        self.directory = directory
        self.summary = summary
        self.frame = CoordinateFrame.from_summary(summary)
        # Archive compatibility: results made before surface units existed keep
        # the raw rail unit and offset-applied display values.
        self.surface_unit = summary.get("surface_unit", summary["scale"]["rail_unit"])
        self.positions = self._plane_values()
        self.unit = self.surface_unit
        self.depth = self._layer("depth")
        self.confidence = self._layer("confidence")
        self.winner = self._layer("selected")
        self.alternative = self._layer("alternate")
        self.reliable = self._layer("reliable")
        self.interpolated = self._layer("interpolated")
        self.valid = self._layer("valid")
        self.contributors = self._layer("contributors")

    def _plane_values(self) -> list:
        rows = self.summary["captures"]
        if not all("display_position" in row for row in rows):
            offset = self.summary["scale"].get("rail_offset", 0.0)
            return [float(row["position"]) - offset for row in rows]
        # A missing display position means the plane sits outside a calibrated
        # rail table, so readings that depend on that plane stay empty.
        return [None if row["display_position"] is None else float(row["display_position"]) for row in rows]

    def plane_value(self, index: int):
        value = self.positions[index]
        return None if value is None or not np.isfinite(value) else float(value)

    def known_positions(self) -> list:
        return [value for value in self.positions if value is not None and np.isfinite(value)]

    def _layer(self, name: str) -> np.ndarray:
        path = self.directory / (name + ".npy")
        if not path.is_file():
            raise Missing("This result's measurement data is missing.")
        return np.load(path, mmap_mode="r", allow_pickle=False)

    def sample(self, point: tuple[float, float], *, trace: bool = True) -> dict:
        x, y = self.frame.local_pixel(point)
        reliable = bool(self.reliable[y, x])
        visible = bool(self.valid[y, x])
        focus_index = int(self.winner[y, x]) if visible else None
        alternate = int(self.alternative[y, x]) if visible else None
        value = {
            **self.frame.xy_summary(x, y),
            "unit": self.unit,
            "position": float(self.depth[y, x]) if reliable else None,
            "confidence": float(self.confidence[y, x]) if visible else None,
            "interpolated": bool(self.interpolated[y, x]),
            "support": int(self.contributors[y, x]),
            "reliable": reliable,
            "visible": visible,
            "capture_index": focus_index,
            "capture_id": self.summary["captures"][focus_index]["id"] if focus_index is not None else None,
            "runner_up": self.plane_value(alternate) if alternate is not None and alternate != focus_index else None,
        }
        if trace:
            value["response"] = peak_path_from_volume(
                self._layer("response"), self._layer("response-support"), self.positions, x, y,
            )
        return value

    def cross_section(self, points: list, steps: int) -> dict:
        start, end = points
        distance = self.frame.distance(start, end)
        if distance <= 0.25:
            raise Unprocessable("Click two different points to make a section.")
        param = np.linspace(0, 1, steps)
        samples = []
        for ratio in param:
            point = (
                (1 - ratio) * start[0] + ratio * end[0],
                (1 - ratio) * start[1] + ratio * end[1],
            )
            sample = self.sample(point, trace=False)
            sample["distance"] = float(ratio * distance)
            sample["distance_um"] = (
                None if self.frame.pixel_um is None
                else sample["distance"] * self.frame.pixel_um
            )
            samples.append(sample)
        counts = sum(sample["reliable"] for sample in samples)
        reliable = [sample["position"] for sample in samples if sample["reliable"]]
        return {
            "unit": self.unit,
            "length_px": distance,
            "length_um": None if self.frame.pixel_um is None else distance * self.frame.pixel_um,
            "reliable_count": counts,
            "reliable_fraction": counts / steps,
            "minimum": min(reliable) if reliable else None,
            "maximum": max(reliable) if reliable else None,
            "samples": samples,
        }

    def _histogram(self, values: np.ndarray, bins: int) -> dict:
        measured = self.known_positions()
        if not measured:
            return {"edges": [], "counts": []}
        lower, higher = min(measured), max(measured)
        edges = np.linspace(lower, higher, bins + 1)
        clipped = np.clip(values, lower, higher)
        counts, _ = np.histogram(clipped, bins=edges)
        return {"edges": edges.tolist(), "counts": counts.tolist()}

    def summarize_mask(
        self,
        viewport: tuple[slice, slice],
        mask: np.ndarray,
        bins: int = 18,
    ) -> dict:
        depths = self.depth[viewport]
        visible = self.valid[viewport] & mask
        reliable = self.reliable[viewport] & mask
        marked = int(np.count_nonzero(mask))
        measured = int(reliable.sum())
        visible_count = int(visible.sum())
        areas = self.frame.pixel_area_um2
        occupied = np.bincount(self.winner[viewport][reliable].astype(np.int32),
                               minlength=len(self.positions))
        summaries = percentile_summary(depths, reliable)
        return {
            "unit": self.unit,
            "pixels": marked,
            "visible_pixels": visible_count,
            "reliable_pixels": measured,
            "reliable_fraction": measured / marked if marked else 0.0,
            "coverage_fraction": visible_count / marked if marked else 0.0,
            "source_area_um2": None if areas is None else marked * areas,
            "reliable_area_um2": None if areas is None else measured * areas,
            "interpolated_pixels": int(np.count_nonzero(self.interpolated[viewport] & mask)),
            "positions": summaries,
            "range": None if not measured else summaries["max"] - summaries["min"],
            "confidence": percentile_summary(self.confidence[viewport], visible),
            "histogram": self._histogram(depths[reliable], bins),
            "frame_occupancy": [
                {"capture_id": capture["id"], "position": self.plane_value(index),
                 "pixels": int(occupied[index])}
                for index, capture in enumerate(self.summary["captures"])
            ],
        }

    def area(self, region: Region, bins: int) -> dict:
        left, top, right, bottom = region.pixels(self.frame.width, self.frame.height)
        viewport = slice(top, bottom), slice(left, right)
        result = self.summarize_mask(viewport, np.ones((bottom - top, right - left), dtype=bool), bins)
        result.update({
            "region": region.public(),
            "pixel_bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
            "response": aggregate_response_window(
                self._layer("response"), self._layer("response-support"), self.positions, *viewport,
            ),
        })
        return result


def polygon_bounds(vertices: list, frame: CoordinateFrame) -> tuple[slice, slice]:
    points = np.asarray(vertices, dtype=np.float64)
    left = max(0, math.floor(float(points[:, 0].min()) * frame.width))
    right = min(frame.width, math.ceil(float(points[:, 0].max()) * frame.width))
    top = max(0, math.floor(float(points[:, 1].min()) * frame.height))
    bottom = min(frame.height, math.ceil(float(points[:, 1].max()) * frame.height))
    return slice(top, bottom), slice(left, right)


def polygon_pixels(vertices: list, frame: CoordinateFrame) -> tuple[tuple[slice, slice], np.ndarray]:
    y_slice, x_slice = polygon_bounds(vertices, frame)
    x = (np.arange(x_slice.start, x_slice.stop) + 0.5) / frame.width
    y = (np.arange(y_slice.start, y_slice.stop) + 0.5)[:, None] / frame.height
    inside = np.zeros((len(y), len(x)), dtype=bool)
    for start, end in zip(vertices, (*vertices[1:], vertices[0])):
        start_x, start_y = start
        end_x, end_y = end
        if end_y == start_y:
            continue
        intersects = (start_y > y) != (end_y > y)
        edge_x = start_x + (y - start_y) * (end_x - start_x) / (end_y - start_y)
        inside ^= intersects & (x < edge_x)
    return (y_slice, x_slice), inside


def polygon_is_simple(points) -> bool:
    def orientation(p1, p2, p3):
        return (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0])

    pairs = list(zip(points, (*points[1:], points[0])))
    for idx, left in enumerate(pairs):
        for other_index in range(idx + 2, len(pairs)):
            if idx == 0 and other_index == len(pairs) - 1:
                continue
            right = pairs[other_index]
            if (max(left[0][0], left[1][0]) < min(right[0][0], right[1][0])
                    or max(right[0][0], right[1][0]) < min(left[0][0], left[1][0])
                    or max(left[0][1], left[1][1]) < min(right[0][1], right[1][1])
                    or max(right[0][1], right[1][1]) < min(left[0][1], left[1][1])):
                continue
            if (orientation(*left, right[0]) * orientation(*left, right[1]) <= 0
                    and orientation(*right, left[0]) * orientation(*right, left[1]) <= 0):
                return False
    return True


def measure_mark(inspection: Inspection, mark: Mark) -> dict:
    if mark.kind == "pin":
        return inspection.sample(mark.vertices[0], trace=False)
    if mark.kind == "line":
        steps = min(160, max(32, math.ceil(inspection.frame.distance(*mark.vertices))))
        value = inspection.cross_section(mark.vertices, steps)
        value.pop("samples")
        return value
    if not polygon_is_simple(mark.vertices):
        raise Unprocessable("The annotation outline crosses itself. Place its points in edge order.")
    viewport, mask = polygon_pixels(mark.vertices, inspection.frame)
    if mask.size == 0 or not mask.any():
        raise Unprocessable("The annotated area contains no output pixel centers.")
    value = inspection.summarize_mask(viewport, mask)
    value.pop("histogram")
    value.pop("frame_occupancy")
    return value


def focus_surface_summary(evidence, depth: np.ndarray) -> dict:
    certain = int(evidence.reliable.sum())
    visible = int(evidence.valid.sum())
    return {
        "reliable_fraction": round(certain / evidence.valid.size, 6),
        "visible_fraction": round(visible / evidence.valid.size, 6),
        "interpolated_fraction": round(float(evidence.interpolated.sum() / max(1, certain)), 6),
        "reliable_pixels": certain,
        "visible_pixels": visible,
        "depth_statistics": percentile_summary(depth, evidence.reliable),
        "confidence_statistics": percentile_summary(evidence.confidence, evidence.valid),
    }
