"""Match capture brightness to the reference through a shared material patch.

The patch is given in normalized reference coordinates, which are the coordinates
of every aligned capture.  A scalar gain per capture is estimated from robust
per-pixel luminance ratios on the patch.  Pixels that are clipped, blocked or not
covered in either capture are ignored, so a patch can be compared where it is
actually observable.  Ratios are combined in a weighted least-squares graph, so a
capture whose patch barely overlaps the reference can still be matched through
one or more intermediate captures.  Captures without a trustworthy path keep
their recorded brightness and are reported by name.
"""

import math
from dataclasses import dataclass

import numpy as np

from .models import Region, Unprocessable

CLIP_LEVEL = 0.985
SHADOW_LEVEL = 0.003
PATCH_SAMPLE_LIMIT = 24_000
MIN_REFERENCE_SAMPLES = 64
MIN_REFERENCE_FRACTION = 0.05
MIN_EDGE_SAMPLES = 32
EDGE_LOG_SIGMA = 0.008
MAX_LOG_SIGMA = 0.04
MAX_BRIDGE_HOPS = 3
MIN_GAIN = 0.1
MAX_GAIN = 10.0
TRIM_LIMIT = 3.0


@dataclass(frozen=True)
class Patch:
    left: int
    top: int
    right: int
    bottom: int
    rows: np.ndarray
    columns: np.ndarray
    grid: tuple[int, int]

    @property
    def samples(self) -> int:
        return int(self.rows.size)

    @property
    def bounds(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]


@dataclass
class LightMatch:
    gains: np.ndarray
    matched: np.ndarray
    report: dict

    def apply(self, rgb: np.ndarray) -> None:
        for index, gain in enumerate(self.gains):
            if not self.matched[index] or abs(float(gain) - 1.0) < 1e-6:
                continue
            np.multiply(rgb[index], float(gain), out=rgb[index])
            np.clip(rgb[index], 0.0, 1.0, out=rgb[index])


def _sample_patch(region: Region, width: int, height: int) -> Patch:
    left, top, right, bottom = region.pixels(width, height)
    area = max(1, (right - left) * (bottom - top))
    step = max(1, math.ceil(math.sqrt(area / PATCH_SAMPLE_LIMIT)))
    rows = np.arange(top, bottom, step, dtype=np.int32)
    columns = np.arange(left, right, step, dtype=np.int32)
    if not rows.size:
        rows = np.array([top], dtype=np.int32)
    if not columns.size:
        columns = np.array([left], dtype=np.int32)
    grid_rows, grid_columns = np.meshgrid(rows, columns, indexing="ij")
    return Patch(
        left, top, right, bottom,
        grid_rows.ravel(), grid_columns.ravel(), (len(rows), len(columns)),
    )


def _edge_estimate(
    first: np.ndarray,
    second: np.ndarray,
    shared: np.ndarray,
    grid: tuple[int, int],
) -> tuple[float, float]:
    """Robust log ratio ``log(second) - log(first)`` and its uncertainty.

    Ratios are trimmed in log space to reject residuals and specular flecks,
    then compared as trimmed means of linear signal.  A mean ratio stays stable
    when one capture is less sharply focused than the other, which a median of
    logs does not guarantee on textured material.  Uncertainty comes from the
    scatter of independent spatial blocks, not from per-pixel texture noise.
    """
    ratio = np.log(np.maximum(second[shared], 1e-7) / np.maximum(first[shared], 1e-7))
    middle = float(np.median(ratio))
    deviation = 1.4826 * float(np.median(np.abs(ratio - middle)))
    limit = max(TRIM_LIMIT * deviation, EDGE_LOG_SIGMA)
    kept = np.abs(ratio - middle) <= limit
    if int(kept.sum()) < MIN_EDGE_SAMPLES:
        kept = np.ones(ratio.shape, dtype=bool)
    upper = float(np.mean(second[shared][kept]))
    lower = float(np.mean(first[shared][kept]))
    if lower <= 1e-7 or upper <= 1e-7:
        return 0.0, MAX_LOG_SIGMA * 2
    center = math.log(upper / lower)
    blocks = _block_ratios(first, second, shared, grid)
    if blocks.size >= 4:
        spread = 1.4826 * float(np.median(np.abs(blocks - np.median(blocks))))
        relative = spread / math.sqrt(blocks.size)
    else:
        relative = MAX_LOG_SIGMA
    return center, max(EDGE_LOG_SIGMA, relative)


def _block_ratios(
    first: np.ndarray,
    second: np.ndarray,
    shared: np.ndarray,
    grid: tuple[int, int],
) -> np.ndarray:
    """Per-block log ratios on a coarse grid of the shared patch samples."""
    rows, columns = grid
    values = []
    for row_block in np.array_split(np.arange(rows), min(4, rows)):
        for column_block in np.array_split(np.arange(columns), min(4, columns)):
            patch = np.ix_(row_block, column_block)
            mask = shared[patch]
            if int(mask.sum()) < 4:
                continue
            upper = float(np.mean(second[patch][mask]))
            lower = float(np.mean(first[patch][mask]))
            if lower > 1e-7 and upper > 1e-7:
                values.append(math.log(upper / lower))
    return np.asarray(values, dtype=np.float64)


def _components(count: int, adjacency: np.ndarray, start: int) -> dict[int, int]:
    hops = {start: 0}
    queue = [start]
    while queue:
        node = queue.pop(0)
        for other in range(count):
            if adjacency[node, other] and other not in hops:
                hops[other] = hops[node] + 1
                queue.append(other)
    return hops


def _solve(
    edges: list[tuple[int, int, float, float]],
    members: dict[int, int],
    reference: int,
) -> tuple[dict[int, float], dict[int, float]]:
    """Weighted least squares on log gains with the reference fixed at zero."""
    free = [index for index in sorted(members) if index != reference]
    if not free:
        return {reference: 0.0}, {reference: 0.0}
    position = {index: offset for offset, index in enumerate(free)}
    matrix = []
    weights = []
    values = []
    for first, second, difference, sigma in edges:
        if first not in members or second not in members:
            continue
        row = np.zeros(len(free), dtype=np.float64)
        if first in position:
            row[position[first]] += 1.0
        if second in position:
            row[position[second]] -= 1.0
        matrix.append(row)
        weights.append(1.0 / max(sigma * sigma, 1e-12))
        values.append(difference)
    if not matrix:
        return {reference: 0.0}, {reference: 0.0}
    matrix = np.asarray(matrix, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    normal = matrix.T @ (matrix * weights[:, None])
    try:
        solution = np.linalg.solve(normal, matrix.T @ (weights * values))
        covariance = np.linalg.inv(normal)
    except np.linalg.LinAlgError:
        return {reference: 0.0}, {reference: 0.0}
    centers = {reference: 0.0}
    errors = {reference: 0.0}
    for index in free:
        offset = position[index]
        if not np.isfinite(solution[offset]):
            continue
        centers[index] = float(solution[offset])
        errors[index] = math.sqrt(max(float(covariance[offset, offset]), 0.0))
    return centers, errors


def _warnings(report: dict) -> list[dict]:
    missing = [row["name"] for row in report["captures"] if row["status"] == "unmatched"]
    if not missing:
        return []
    detail = (
        f"No trustworthy light comparison for {len(missing)} capture"
        f"{'' if len(missing) == 1 else 's'}; left out of this reconstruction: "
        f"{', '.join(missing)}. The original files remain in the stack."
    )
    return [{
        "code": "lighting",
        "detail": detail,
        "capture_ids": [
            row["id"] for row in report["captures"] if row["status"] == "unmatched"
        ],
    }]


def match_lighting(
    rgb: np.ndarray,
    coverage: np.ndarray,
    reference: int,
    region: Region | None,
    captures: list[dict],
) -> LightMatch | None:
    """Estimate and apply one exposure gain per aligned capture.

    ``rgb`` and ``coverage`` hold all captures in stack order; ``rgb`` gains are
    applied in place.  The reference capture defines the target brightness and is
    never changed.  ``LightMatch.usable`` marks the captures with a trustworthy
    comparison; others must be left out of fusion and are named in the warning.
    """
    if region is None:
        return None
    count, height, width = rgb.shape[:3]
    patch = _sample_patch(region, width, height)
    grid_rows, grid_columns = patch.grid
    values = rgb[:, patch.rows, patch.columns, :].reshape(count, grid_rows, grid_columns, 3)
    luminance = np.clip(
        values[..., 0] * 0.2126 + values[..., 1] * 0.7152 + values[..., 2] * 0.0722, 0.0, 1.0,
    )
    clipped = np.max(values, axis=3) >= CLIP_LEVEL
    covered = coverage[:, patch.rows, patch.columns].reshape(count, grid_rows, grid_columns)
    usable = covered & ~clipped & (luminance > SHADOW_LEVEL) & np.isfinite(luminance)
    usable_samples = usable.sum(axis=(1, 2))
    area = max(patch.samples, 1)
    required = max(MIN_REFERENCE_SAMPLES, math.ceil(MIN_REFERENCE_FRACTION * area))
    if int(usable_samples[reference]) < required:
        raise Unprocessable(
            "The light-matching patch has too few usable samples in the reference capture. "
            "Select a larger, evenly lit area of unclipped material.",
            field="lighting",
            details={
                "usable_samples": int(usable_samples[reference]),
                "required_samples": required,
                "bounds": patch.bounds,
            },
        )

    edges = []
    direct = np.zeros((count, count), dtype=np.int64)
    for first in range(count):
        for second in range(first + 1, count):
            shared = usable[first] & usable[second]
            samples = int(shared.sum())
            direct[first, second] = direct[second, first] = samples
            if samples < MIN_EDGE_SAMPLES:
                continue
            difference, sigma = _edge_estimate(
                luminance[first], luminance[second], shared, patch.grid,
            )
            edges.append((first, second, difference, sigma))

    adjacency = direct >= MIN_EDGE_SAMPLES
    hops = _components(count, adjacency, reference)
    centers, errors = _solve(edges, hops, reference)

    gains = np.ones(count, dtype=np.float64)
    matched = np.zeros(count, dtype=bool)
    rows = []
    corrected = 0
    for index, capture in enumerate(captures):
        status = "unmatched"
        reason = None
        gain = 1.0
        sigma = 0.0
        if index == reference:
            status = "reference"
        elif index not in centers:
            if int(usable_samples[index]) == 0:
                reason = "The patch has no usable samples in this capture."
            elif not np.any(adjacency[index]):
                reason = "Too few usable patch samples are shared with the other captures."
            else:
                reason = "No trustworthy chain of shared patch samples reaches the reference."
        else:
            gain = math.exp(centers[index])
            sigma = errors.get(index, 0.0)
            if hops.get(index, MAX_BRIDGE_HOPS + 1) > MAX_BRIDGE_HOPS:
                reason = (
                    "The patch reaches this capture only through more than "
                    f"{MAX_BRIDGE_HOPS} intermediate frames."
                )
            elif not MIN_GAIN <= gain <= MAX_GAIN:
                reason = "The estimated gain is outside the supported range."
            elif sigma > MAX_LOG_SIGMA:
                reason = "Comparisons through other captures are too uncertain."
            else:
                status = (
                    "direct" if int(direct[reference, index]) >= MIN_EDGE_SAMPLES else "bridged"
                )
        if status in ("reference", "direct", "bridged"):
            gains[index] = gain
            matched[index] = True
            corrected += status != "reference"
        else:
            gain = 1.0
        rows.append({
            "index": index,
            "id": capture["id"],
            "name": capture["name"],
            "gain": round(float(gain), 6),
            "log_sigma": round(float(sigma), 6),
            "status": status,
            "hops": int(hops.get(index, 0)),
            "direct_samples": int(direct[reference, index]),
            "usable_samples": int(usable_samples[index]),
            "usable_fraction": round(float(usable_samples[index]) / area, 6),
            "reason": reason,
        })

    report = {
        "model": "gain",
        "patch": region.public(),
        "bounds": patch.bounds,
        "reference_id": captures[reference]["id"],
        "reference_name": captures[reference]["name"],
        "samples": patch.samples,
        "edge_count": len(edges),
        "corrected": int(corrected),
        "unmatched": int(count - 1 - corrected),
        "excluded_ids": [
            row["id"] for row in rows if row["status"] == "unmatched"
        ],
        "captures": rows,
    }
    report["warnings"] = _warnings(report)
    match = LightMatch(gains, matched, report)
    match.apply(rgb)
    return match


def capture_gain(report: dict | None, capture_id: str) -> float:
    """Return the archived gain for one capture, or 1.0 when none was applied."""
    if not report:
        return 1.0
    for row in report.get("captures", []):
        if row.get("id") == capture_id and row.get("status") in ("reference", "direct", "bridged"):
            return float(row.get("gain", 1.0))
    return 1.0


def apply_gain(rgb: np.ndarray, gain: float) -> np.ndarray:
    if abs(gain - 1.0) < 1e-6:
        return rgb
    return np.clip(rgb * gain, 0.0, 1.0)
