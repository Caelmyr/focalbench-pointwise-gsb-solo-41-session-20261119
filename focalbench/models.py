"""Input validation and domain records shared by the HTTP and reconstruction layers."""

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class WorkbenchError(Exception):
    status = 400

    def __init__(self, message: str, *, field: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details

    def public(self) -> dict:
        value = {"error": self.message}
        if self.field:
            value["field"] = self.field
        if self.details is not None:
            value["details"] = self.details
        return value


class Missing(WorkbenchError):
    status = 404


class Conflict(WorkbenchError):
    status = 409


class Capacity(WorkbenchError):
    status = 413


class Unprocessable(WorkbenchError):
    status = 422


MAX_FRAMES = 64
MAX_ACTIVE_FRAMES = 32
MAX_MARKS = 80
MIN_FRAMES = 3
DEFAULT_NAME = "Untitled focus stack"
UNITS = ("µm", "mm", "step")
IMAGE_EXTENSIONS = frozenset((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"))
RUN_ASSETS = {
    "composite": "composite.png",
    "depth": "surface.png",
    "confidence": "confidence.png",
    "coverage": "coverage.png",
    "plane": "focus-plane.png",
    "overlap": "overlap.png",
    "depth16": "depth-16.png",
    "overview": "overview.webp",
}


def storage_id() -> str:
    return uuid4().hex


def require_id(value: Any, label: str = "id") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise Missing(f"Unknown {label}.")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def object_value(value: Any, label: str = "request") -> dict:
    if not isinstance(value, dict):
        raise WorkbenchError(f"{label.capitalize()} must be an object.", field=label)
    return value


def validate_keys(value: dict, allowed: set, label: str = "request") -> None:
    unexpected = sorted(set(value).difference(allowed))
    if unexpected:
        raise WorkbenchError(f"Unknown {label} field.", field=unexpected[0])


def number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise WorkbenchError(f"{label.capitalize()} must be a number.", field=label)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise WorkbenchError(f"{label.capitalize()} must be a number.", field=label) from error
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise WorkbenchError(
            f"{label.capitalize()} must be between {low:g} and {high:g}.",
            field=label,
        )
    return parsed


def integer(value: Any, label: str, low: int, high: int) -> int:
    result = number(value, label, low, high)
    if int(result) != result:
        raise WorkbenchError(f"{label.capitalize()} must be a whole number.", field=label)
    return int(result)


def choice(value: Any, label: str, options: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in options:
        raise WorkbenchError(
            f"{label.capitalize()} must be one of {', '.join(options)}.",
            field=label,
        )
    return value


def flag(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise WorkbenchError(f"{label.capitalize()} must be true or false.", field=label)
    return value


def short_text(
    value: Any,
    label: str,
    *,
    maximum: int = 90,
    allow_blank: bool = False,
) -> str:
    if not isinstance(value, str):
        raise WorkbenchError(f"{label.capitalize()} must be text.", field=label)
    if not allow_blank and not value.strip():
        raise WorkbenchError(f"Enter {label}.", field=label)
    value = value.strip()
    if len(value) > maximum or any(ord(char) < 32 and char != "\n" for char in value):
        raise WorkbenchError(
            f"{label.capitalize()} must be at most {maximum} characters and contain valid text.",
            field=label,
        )
    return value


def capture_name(value: Any) -> str:
    clean = short_text(value, "filename", maximum=220)
    path = Path(clean.replace("\\", "/"))
    if path.suffix.casefold() not in IMAGE_EXTENSIONS:
        raise Unprocessable("Choose PNG, JPEG, TIFF or WebP capture files.", field="filename")
    return path.name


def relative_coordinate(value: Any, label: str) -> float:
    return number(value, label, 0.0, 1.0)


def parse_vertex(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise WorkbenchError(f"{label.capitalize()} must contain x and y.", field=label)
    return relative_coordinate(value[0], f"{label} x"), relative_coordinate(value[1], f"{label} y")


@dataclass(frozen=True)
class Region:
    """Half-open normalized rectangle in the displayed reference view."""

    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "region")
        names = ("left", "top", "right", "bottom")
        validate_keys(raw, set(names), "region")
        edges = {label: relative_coordinate(raw.get(label), label) for label in names}
        if edges["right"] - edges["left"] < 0.005 or edges["bottom"] - edges["top"] < 0.005:
            raise WorkbenchError("Select a region at least 0.5% wide and tall.", field="region")
        return cls(**edges)

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            max(0, min(width - 1, math.floor(self.left * width))),
            max(0, min(height - 1, math.floor(self.top * height))),
            max(1, min(width, math.ceil(self.right * width))),
            max(1, min(height, math.ceil(self.bottom * height))),
        )

    def public(self) -> dict:
        return asdict(self)


MAX_RAIL_POINTS = 12
RAIL_MODES = ("linear", "calibration")


@dataclass(frozen=True)
class RailCalibration:
    """Measured pairs of stage steps and actual focus height in micrometres.

    The points must increase in both coordinates. Heights between marks are
    interpolated linearly, so spans with different um-per-step ratios are kept
    exact at every measured mark. Steps outside the first and last mark have no
    calibrated height.
    """

    points: tuple

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "rail calibration")
        validate_keys(raw, {"points"}, "rail calibration")
        rows = raw.get("points")
        if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_RAIL_POINTS:
            raise WorkbenchError(
                f"Enter between 2 and {MAX_RAIL_POINTS} step and height pairs.",
                field="rail_calibration",
            )
        points = []
        for index, row in enumerate(rows, 1):
            label = f"rail calibration pair {index}"
            if isinstance(row, (list, tuple)) and len(row) == 2:
                step = number(row[0], f"step on pair {index}", -1e7, 1e7)
                um = number(row[1], f"height on pair {index}", -1e7, 1e7)
                points.append((step, um))
                continue
            raw_row = object_value(row, label)
            validate_keys(raw_row, {"step", "um"}, label)
            step = number(raw_row.get("step"), f"step on pair {index}", -1e7, 1e7)
            um = number(raw_row.get("um"), f"height on pair {index}", -1e7, 1e7)
            points.append((step, um))
        for index in range(1, len(points)):
            step, um = points[index]
            last_step, last_um = points[index - 1]
            if step <= last_step:
                raise WorkbenchError(
                    "Rail calibration steps must increase from one pair to the next.",
                    field="rail_calibration",
                )
            if um <= last_um:
                raise WorkbenchError(
                    "Rail calibration heights must increase from one pair to the next.",
                    field="rail_calibration",
                )
        return cls(tuple(points))

    def height_at(self, step: float) -> float | None:
        points = self.points
        if step < points[0][0] or step > points[-1][0]:
            return None
        index = 1
        while index < len(points) - 1 and step > points[index][0]:
            index += 1
        (left_step, left_um), (right_step, right_um) = points[index - 1], points[index]
        ratio = (step - left_step) / (right_step - left_step)
        return left_um + ratio * (right_um - left_um)

    def step_at(self, height: float) -> float | None:
        points = self.points
        if height < points[0][1] or height > points[-1][1]:
            return None
        index = 1
        while index < len(points) - 1 and height > points[index][1]:
            index += 1
        (left_step, left_um), (right_step, right_um) = points[index - 1], points[index]
        ratio = (height - left_um) / (right_um - left_um)
        return left_step + ratio * (right_step - left_step)

    def public(self) -> dict:
        return {"points": [{"step": step, "um": um} for step, um in self.points]}


@dataclass(frozen=True)
class CaptureScale:
    """Calibration in original reference-image pixels with separate XY and rail units.

    ``rail_mode == "calibration"`` replaces the offset-only rail reading with a
    measured step-to-height table; the surface unit is then always micrometres.
    """

    pixel_um: float | None = None
    rail_unit: str = "µm"
    rail_offset: float = 0.0
    rail_mode: str = "linear"
    calibration: RailCalibration | None = None

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "scale")
        validate_keys(
            raw, {"pixel_um", "rail_unit", "rail_offset", "rail_mode", "calibration"}, "scale",
        )
        pixel = raw.get("pixel_um")
        calibration_row = raw.get("calibration")
        calibration = None
        if calibration_row not in (None, {}):
            calibration = RailCalibration.parse(calibration_row)
        mode = choice(raw.get("rail_mode", "linear"), "rail scale", RAIL_MODES)
        if mode == "calibration" and calibration is None:
            raise WorkbenchError(
                "Enter at least two step and height pairs for a calibrated rail scale.",
                field="rail_calibration",
            )
        return cls(
            pixel_um=None if pixel is None else number(pixel, "pixel size", 0.001, 100000.0),
            # Measured pairs are keyed by stage steps, so entry stays in steps
            # while every surface reading is reported in micrometres.
            rail_unit="step" if mode == "calibration" else choice(
                raw.get("rail_unit", "µm"), "rail unit", ("µm", "mm", "step"),
            ),
            rail_offset=number(raw.get("rail_offset", 0), "rail offset", -10_000_000, 10_000_000),
            rail_mode=mode,
            calibration=calibration,
        )

    @property
    def calibrated(self) -> bool:
        return self.rail_mode == "calibration" and self.calibration is not None

    @property
    def surface_unit(self) -> str:
        return "µm" if self.calibrated else self.rail_unit

    def display_position(self, position: float) -> float:
        if self.calibrated:
            height = self.calibration.height_at(position)
            return float("nan") if height is None else height
        return position - self.rail_offset

    def public(self) -> dict:
        value = {
            "pixel_um": self.pixel_um,
            "rail_unit": self.rail_unit,
            "rail_offset": self.rail_offset,
            "rail_mode": self.rail_mode,
            "calibration": None if self.calibration is None else self.calibration.public(),
        }
        return value


@dataclass(frozen=True)
class CapturePlan:
    first: float
    last: float
    in_focus_width: float
    overlap_percent: int
    usable_frames: int
    settle_ms: int
    interval_ms: int

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "plan")
        validate_keys(raw, {
            "first", "last", "in_focus_width", "overlap_percent",
            "usable_frames", "settle_ms", "interval_ms",
        }, "plan")
        first = number(raw.get("first"), "first position", -1e7, 1e7)
        last = number(raw.get("last"), "last position", -1e7, 1e7)
        if last == first:
            raise WorkbenchError("Start and end positions must differ.", field="last")
        return cls(
            first=first,
            last=last,
            in_focus_width=number(raw.get("in_focus_width"), "sharp interval", 0.00001, 1e7),
            overlap_percent=integer(raw.get("overlap_percent", 40), "overlap", 0, 90),
            usable_frames=integer(raw.get("usable_frames", 24), "frame budget", 3, MAX_ACTIVE_FRAMES),
            settle_ms=integer(raw.get("settle_ms", 300), "settle time", 0, 60_000),
            interval_ms=integer(raw.get("interval_ms", 200), "capture time", 1, 60_000),
        )


@dataclass(frozen=True)
class RunSettings:
    registration: str = "breathing"
    reference_id: str | None = None
    score: str = "balanced"
    blend: str = "feather"
    viewport: str = "reference"
    scale: CaptureScale = field(default_factory=CaptureScale)
    confidence_floor: float = 0.16
    edge_guard: int = 3
    strength: float = 0.7

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "settings")
        validate_keys(raw, {
            "registration", "reference_id", "score", "blend", "viewport",
            "scale", "confidence_floor", "edge_guard", "strength",
        }, "settings")
        reference = raw.get("reference_id")
        return cls(
            registration=choice(
                raw.get("registration", "breathing"), "registration",
                ("none", "translation", "breathing"),
            ),
            reference_id=None if reference in ("", None) else require_id(reference, "reference frame"),
            score=choice(raw.get("score", "balanced"), "focus response", ("fine", "balanced", "broad")),
            blend=choice(raw.get("blend", "feather"), "fusion", ("winner", "feather", "pyramid")),
            viewport=choice(raw.get("viewport", "reference"), "viewport", ("reference", "shared")),
            scale=CaptureScale.parse(raw.get("scale", {})),
            confidence_floor=number(raw.get("confidence_floor", 0.16), "confidence floor", 0, 0.95),
            edge_guard=integer(raw.get("edge_guard", 3), "edge guard", 0, 12),
            strength=number(raw.get("strength", 0.7), "fusion strength", 0.1, 1.0),
        )

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FrameEdit:
    position: float | None = None
    enabled: bool | None = None
    note: str | None = None

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "capture edit")
        validate_keys(raw, {"position", "enabled", "note"}, "capture edit")
        if not raw:
            raise WorkbenchError("Supply a capture position, state or note.")
        return cls(
            position=number(raw["position"], "focus position", -1e7, 1e7) if "position" in raw else None,
            enabled=flag(raw["enabled"], "included") if "enabled" in raw else None,
            note=short_text(raw["note"], "note", maximum=600, allow_blank=True) if "note" in raw else None,
        )


MARK_COLORS = ("amber", "cyan", "magenta", "white")
MARK_KINDS = ("pin", "line", "area")


@dataclass(frozen=True)
class Mark:
    kind: str
    label: str
    color: str
    vertices: tuple[tuple[float, float], ...]
    note: str

    @classmethod
    def parse(cls, payload: Any):
        raw = object_value(payload, "annotation")
        validate_keys(raw, {"kind", "label", "color", "vertices", "note"}, "annotation")
        kind = choice(raw.get("kind"), "annotation kind", MARK_KINDS)
        vertices = raw.get("vertices")
        minimum, maximum = {"pin": (1, 1), "line": (2, 2), "area": (3, 24)}[kind]
        if not isinstance(vertices, (tuple, list)) or not minimum <= len(vertices) <= maximum:
            expectation = str(minimum) if minimum == maximum else f"{minimum}–{maximum}"
            raise WorkbenchError(f"This annotation needs {expectation} vertices.", field="vertices")
        points = tuple(parse_vertex(vertex, f"point {i + 1}") for i, vertex in enumerate(vertices))
        if len(set(points)) < minimum:
            raise WorkbenchError("Place the annotation points apart.", field="vertices")
        if kind == "line":
            distance = math.dist(*points)
            if distance < 0.002:
                raise WorkbenchError("Draw a longer measurement line.", field="vertices")
        return cls(
            kind=kind,
            label=short_text(raw.get("label"), "annotation label", maximum=64),
            color=choice(raw.get("color", "amber"), "annotation color", MARK_COLORS),
            vertices=points,
            note=short_text(raw.get("note", ""), "note", maximum=600, allow_blank=True),
        )

    def public(self) -> dict:
        return asdict(self)


def parse_points_query(points: Any, *, minimum: int = 2, maximum: int = 2) -> list:
    if not isinstance(points, list) or not minimum <= len(points) <= maximum:
        raise WorkbenchError(f"Choose {minimum}–{maximum} sample points.", field="points")
    return [parse_vertex(point, f"point {index + 1}") for index, point in enumerate(points)]


def require_frame_count(count: int) -> None:
    if count < MIN_FRAMES:
        raise Unprocessable(f"Enable at least {MIN_FRAMES} captures before reconstructing.")
    if count > MAX_ACTIVE_FRAMES:
        raise Capacity(f"Enable at most {MAX_ACTIVE_FRAMES} captures per result.")
