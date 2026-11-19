"""Estimate observed sharp intervals from archived focus response traces."""

from dataclasses import dataclass

import numpy as np

from .models import Region, Unprocessable
from .surface import Inspection

MIN_PROFILE_SAMPLES = 3


@dataclass(frozen=True)
class SharpInterval:
    lo: float | None
    hi: float | None
    lowest_sample: float
    highest_sample: float
    peak_position: float
    peak_score: float
    captures: int

    def public(self) -> dict:
        return {
            "low": self.lo,
            "high": self.hi,
            "first_sample": self.lowest_sample,
            "last_sample": self.highest_sample,
            "peak_position": self.peak_position,
            "peak_response": self.peak_score,
            "captures": self.captures,
            "bracketed": self.lo is not None and self.hi is not None,
            "width": self.hi - self.lo if self.lo is not None and self.hi is not None else None,
        }


def _crossing(x0: float, y0: float, x1: float, y1: float, target: float) -> float:
    if y0 == y1:
        return x0
    return float(x0 + (x1 - x0) * np.clip((target - y0) / (y1 - y0), 0, 1))


def _supported_spans(above: np.ndarray):
    indices = np.flatnonzero(np.diff(np.pad(above.astype(np.int8), [1, 1])))
    return zip(indices[::2], indices[1::2])


def _interval(start: int, stop: int, stages: np.ndarray, score: np.ndarray, gate: float) -> SharpInterval:
    tail = stop - 1
    low = None
    high = None
    if start and np.isfinite(score[start - 1]):
        low = _crossing(stages[start - 1], score[start - 1], stages[start], score[start], gate)
    if stop < stages.size and np.isfinite(score[stop]):
        high = _crossing(stages[tail], score[tail], stages[stop], score[stop], gate)
    peak = start + int(np.argmax(score[start:stop]))
    return SharpInterval(
        low, high, float(stages[start]), float(stages[tail]), float(stages[peak]),
        float(score[peak]), int(stop - start),
    )


def _window_summary(inspection: Inspection, region: Region) -> tuple[dict, np.ndarray, np.ndarray]:
    observations = inspection.area(region, bins=12)
    response = observations["response"]
    expected = observations["pixels"]
    valid = np.asarray([item["valid_count"] for item in response])
    stages = np.asarray([item["position"] for item in response], dtype=np.float64)
    score = np.asarray([np.nan if item["p75"] is None else item["p75"] for item in response], dtype=np.float64)
    score[valid < max(4, expected * 0.25)] = np.nan
    return observations, stages, score


def characterize_focus(inspection: Inspection, region: Region, fraction: float) -> dict:
    observations, stages, score = _window_summary(inspection, region)
    finite = np.isfinite(score)
    if int(finite.sum()) < MIN_PROFILE_SAMPLES:
        raise Unprocessable("Choose a patch with at least three well-covered focus positions.")
    peak = float(np.nanmax(score))
    baseline = float(np.nanpercentile(score, 15))
    dynamic_span = peak - baseline
    gate = baseline + dynamic_span * fraction
    active = finite & (score >= gate)
    spans = [_interval(start, stop, stages, score, gate) for start, stop in _supported_spans(active)]
    best = max(spans, key=lambda item: (item.peak_score, item.captures))
    stable = best.lo is not None and best.hi is not None and dynamic_span >= 0.0012
    warnings = []
    if dynamic_span < 0.0012:
        warnings.append("This patch has little focus variation across the observed rail.")
    if best.lo is None or best.hi is None:
        warnings.append("The strongest interval reaches a stack edge or a poorly covered position.")
    if any(span is not best and span.peak_score >= best.peak_score * 0.85 for span in spans):
        warnings.append("More than one sharp band is visible in this patch.")
        stable = False
    return {
        "selection": observations["region"],
        "unit": inspection.unit,
        "measured_frames": int(finite.sum()),
        "minimum_fraction": fraction,
        "baseline": baseline,
        "response_gate": gate,
        "maximum": peak,
        "safe_width_for_plan": best.hi - best.lo if stable else None,
        "strongest": best.public(),
        "bands": [item.public() for item in spans],
        "curve": [
            {"position": float(position) if np.isfinite(position) else None,
             "response": float(score[index]) if finite[index] else None}
            for index, position in enumerate(stages)
        ],
        "warnings": warnings,
    }
