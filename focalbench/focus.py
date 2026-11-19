"""Focus response, focal-plane evidence and continuous surface reconstruction."""

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .imaging import fill_nearest, valid_neighborhood

SCORE_SCALES = {
    "fine": (0.62, 1.8, 0.78),
    "balanced": (0.88, 3.0, 0.59),
    "broad": (1.20, 5.0, 0.38),
}
RESPONSE_NOISE = 0.0012
TINY = 1e-8


@dataclass
class FocusEvidence:
    selected: np.ndarray
    alternate: np.ndarray
    depth: np.ndarray
    confidence: np.ndarray
    valid: np.ndarray
    reliable: np.ndarray
    interpolated: np.ndarray
    support_count: np.ndarray
    response_at_peak: np.ndarray


def local_energy(values: np.ndarray, sigma: float) -> np.ndarray:
    square = values * values
    return np.sqrt(np.maximum(ndimage.gaussian_filter(square, sigma, mode="nearest"), 0))


def derivative_energy(luminance: np.ndarray, sigma: float, pooling: float) -> np.ndarray:
    x = ndimage.gaussian_filter(luminance, sigma, order=(0, 1), mode="nearest")
    y = ndimage.gaussian_filter(luminance, sigma, order=(1, 0), mode="nearest")
    square = x * x + y * y
    return np.sqrt(np.maximum(ndimage.gaussian_filter(square, pooling, mode="nearest"), 0))


def image_response(
    luminance: np.ndarray,
    support: np.ndarray,
    mode: str,
    guard_px: int,
) -> tuple[np.ndarray, np.ndarray]:
    low_sigma, pooling, detail_mix = SCORE_SCALES[mode]
    evidence = valid_neighborhood(support, guard_px)
    filled = fill_nearest(luminance, support)
    smooth = ndimage.gaussian_filter(filled, low_sigma, mode="nearest")
    background = ndimage.gaussian_filter(filled, low_sigma * 2.4, mode="nearest")
    detail = local_energy(smooth - background, pooling)
    edges = derivative_energy(filled, low_sigma * 1.35, pooling)
    background = ndimage.gaussian_filter(filled, pooling * 2.0, mode="nearest")
    normalized = (detail * detail_mix + edges * (1 - detail_mix))
    normalized /= np.sqrt(np.maximum(background, 0.035))
    normalized[~evidence] = 0
    return normalized.astype(np.float32, copy=False), evidence


def _at_stack(planes: np.ndarray, index: np.ndarray) -> np.ndarray:
    return np.take_along_axis(planes, index[None, ...], axis=0)[0]


def peak_competitor(
    scores: np.ndarray,
    usable: np.ndarray,
    selected: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    second_value = np.zeros(selected.shape, dtype=np.float32)
    second_index = selected.copy()
    far_value = np.zeros(selected.shape, dtype=np.float32)
    far_index = selected.copy()
    for stack_index in range(scores.shape[0]):
        plane = scores[stack_index]
        valid = usable[stack_index]
        alternate = valid & (stack_index != selected) & (plane > second_value)
        second_value[alternate] = plane[alternate]
        second_index[alternate] = stack_index
        far = valid & (np.abs(stack_index - selected) > 1) & (plane > far_value)
        far_value[far] = plane[far]
        far_index[far] = stack_index
    alternate_index = np.where(far_value > second_value * 0.77, far_index, second_index)
    return second_value, alternate_index


def confidence_map(
    peak: np.ndarray,
    competitor: np.ndarray,
    support_count: np.ndarray,
    capture_count: int,
) -> np.ndarray:
    prominence = np.maximum(peak - competitor, 0) / np.maximum(peak, TINY)
    discriminability = np.clip(prominence * 4.5, 0, 1)
    texture = np.clip((peak - RESPONSE_NOISE) / (RESPONSE_NOISE * 8), 0, 1)
    coverage = np.clip((support_count.astype(np.float32) - 1) / max(1, min(5, capture_count - 1)), 0.0, 1.0)
    score = np.sqrt(discriminability * texture) * coverage
    return np.asarray(score, dtype=np.float32)


def interpolate_depth(
    scores: np.ndarray,
    usable: np.ndarray,
    selected: np.ndarray,
    positions: np.ndarray,
    display: np.ndarray | None = None,
    outside: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Place each peak between neighbouring planes.

    ``positions`` orders and scales the local peak fit. ``display`` supplies the
    reported value at the same plane locations; it differs from ``positions``
    when the rail is calibrated with unequal step-to-height spans. ``outside``
    marks planes whose displayed height is not calibrated, so a fitted peak that
    is anchored on one of them stays unmeasured instead of extrapolating.
    """
    if display is None:
        display = positions
    if outside is None:
        outside = np.zeros(len(positions), dtype=bool)
    last = len(positions) - 1
    left_index = np.maximum(selected - 1, 0)
    right_index = np.minimum(selected + 1, last)
    depth = display[selected].astype(np.float64)
    left_dx = positions[selected] - positions[left_index]
    right_dx = positions[right_index] - positions[selected]
    eligible = (
        (selected > 0)
        & (selected < last)
        & _at_stack(usable, left_index)
        & _at_stack(usable, right_index)
        & (left_dx > TINY)
        & (right_dx > TINY)
        & ~outside[selected]
        & ~outside[left_index]
        & ~outside[right_index]
    )
    if not eligible.any():
        return depth, eligible

    indices = np.nonzero(eligible)
    center = _at_stack(scores, selected)[indices].astype(np.float64)
    lower = _at_stack(scores, left_index)[indices].astype(np.float64)
    upper = _at_stack(scores, right_index)[indices].astype(np.float64)
    step_left = left_dx[indices]
    step_right = right_dx[indices]
    display_left = display[left_index[indices]].astype(np.float64)
    display_right = display[right_index[indices]].astype(np.float64)
    display_center = display[selected[indices]].astype(np.float64)
    slope_left = (center - lower) / step_left
    slope_right = (upper - center) / step_right
    curvature = (slope_right - slope_left) / (step_left + step_right)
    slope = slope_left + curvature * step_left
    can_fit = (center > RESPONSE_NOISE) & (curvature < -1e-16)
    vertex = np.zeros(center.shape, dtype=np.float64)
    np.divide(-slope, 2 * curvature, out=vertex, where=can_fit)
    can_fit &= (
        (vertex >= -0.65 * step_left)
        & (vertex <= 0.65 * step_right)
        & (np.maximum(lower, upper) >= center * 0.18)
        & (np.maximum(step_left, step_right) <= 4 * np.minimum(step_left, step_right))
    )
    fitted = np.where(vertex >= 0, display_center + vertex / step_right * (display_right - display_center),
                      display_center + vertex / step_left * (display_center - display_left))
    # A calibrated rail can step irregularly; keep a fit inside the span the
    # three samples actually bracket.
    low = np.fmin(display_center, np.fmin(display_left, display_right))
    high = np.fmax(display_center, np.fmax(display_left, display_right))
    can_fit &= (fitted >= low) & (fitted <= high)
    accepted = tuple(axis[can_fit] for axis in indices)
    depth[accepted] = fitted[can_fit]
    eligible[indices] = can_fit
    return depth, eligible


def analyze_peaks(
    scores: np.ndarray,
    coverage: np.ndarray,
    strict_support: np.ndarray,
    positions: np.ndarray,
    floor: float,
    display: np.ndarray | None = None,
) -> FocusEvidence:
    has_strict_support = strict_support.any(axis=0)
    usable = strict_support | (~has_strict_support[None, ...] & coverage)
    support_count = strict_support.sum(axis=0, dtype=np.uint8)
    valid = coverage.any(axis=0)
    weighted = np.where(usable, scores, -1.0)
    picked = weighted.argmax(axis=0).astype(np.int32)
    peak = _at_stack(scores, picked)
    competitor, alternate = peak_competitor(scores, usable, picked)
    outside = np.zeros(len(positions), dtype=bool) if display is None else ~np.isfinite(display)
    depth, interpolated = interpolate_depth(scores, strict_support, picked, positions, display, outside)
    confidence = confidence_map(peak, competitor, support_count, len(positions))
    confidence[~has_strict_support] = 0
    reliable = valid & has_strict_support & (support_count >= 2) & (peak > RESPONSE_NOISE)
    reliable &= confidence >= max(floor, np.finfo(np.float32).eps)
    reliable &= ~outside[picked]
    depth[~valid] = np.nan
    outside_depth = ~np.isfinite(depth) | outside[picked]
    depth[outside_depth] = np.nan
    reliable &= ~outside_depth
    interpolated &= reliable
    return FocusEvidence(
        picked, alternate, depth, confidence, valid, reliable, interpolated,
        support_count, peak,
    )


def plane_distribution(selected: np.ndarray, valid: np.ndarray, count: int) -> np.ndarray:
    if np.any(valid):
        per_plane = np.bincount(selected[valid], minlength=count).astype(np.int64)
    else:
        per_plane = np.zeros(count, dtype=np.int64)
    return per_plane


def frame_evidence(
    scores: np.ndarray,
    strict_support: np.ndarray,
    evidence: FocusEvidence,
) -> list[dict]:
    winner_counts = plane_distribution(evidence.selected, evidence.reliable, len(scores))
    frame_pixels = evidence.valid.size
    records = []
    for index, plane in enumerate(scores):
        valid = strict_support[index]
        samples = plane[valid]
        records.append({
            "valid_fraction": round(float(valid.mean()), 6),
            "median_response": round(float(np.median(samples)), 8) if samples.size else None,
            "p95_response": round(float(np.percentile(samples, 95)), 8) if samples.size else None,
            "winner_count": int(winner_counts[index]),
            "winner_fraction": round(float(winner_counts[index] / frame_pixels), 6),
        })
    return records


def rail_statistics(positions: np.ndarray) -> dict:
    sorted_positions = np.sort(positions)
    intervals = np.diff(sorted_positions)
    if not intervals.size:
        return {"minimum_step": None, "median_step": None, "maximum_step": None, "uniform": None}
    median = float(np.median(intervals))
    average = float(intervals.mean())
    tolerance = max(1e-7, average * 0.015)
    return {
        "minimum_step": float(intervals.min()),
        "median_step": median,
        "maximum_step": float(intervals.max()),
        "uniform": bool(np.ptp(intervals) <= tolerance),
    }


def _readable(position: float) -> float | None:
    return None if position is None or not np.isfinite(position) else float(position)


def peak_path_from_volume(
    response: np.ndarray,
    support: np.ndarray,
    position_list: list[float],
    x: int,
    y: int,
) -> list[dict]:
    path = []
    for plane, position in enumerate(position_list):
        supported = bool(support[plane, y, x])
        path.append({
            "index": plane,
            "position": _readable(position),
            "supported": supported,
            "response": float(response[plane, y, x]) if supported else None,
        })
    return path


def aggregate_response_window(
    response: np.ndarray,
    support: np.ndarray,
    positions: list[float],
    y_slice: slice,
    x_slice: slice,
) -> list[dict]:
    result = []
    for index, position in enumerate(positions):
        mask = support[index, y_slice, x_slice]
        plane = response[index, y_slice, x_slice]
        values = plane[mask]
        result.append({
            "index": index,
            "position": _readable(position),
            "valid_count": int(values.size),
            "mean": float(values.mean(dtype=np.float64)) if values.size else None,
            "p75": float(np.percentile(values, 75)) if values.size else None,
            "maximum": float(values.max()) if values.size else None,
        })
    return result
