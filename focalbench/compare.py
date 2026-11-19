"""Pair inspection of stored captures, raw or using the archived registration."""

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .alignment import AnalysisBox, Motion, warp
from .fusion import covered_image, difference_overlay
from .imaging import fit_size, linear_luminance, linear_to_srgb, percentile_summary, read_pixels
from .lighting import apply_gain, capture_gain
from .models import Missing, Region, Unprocessable


@dataclass
class Pair:
    first_rgb: np.ndarray
    second_rgb: np.ndarray
    first_support: np.ndarray
    second_support: np.ndarray
    first_name: str
    second_name: str
    positions: tuple[float, float]
    transformed: bool
    unit: str

    @property
    def shared(self) -> np.ndarray:
        return self.first_support & self.second_support


def _for_id(run: dict, capture_id: str) -> tuple[dict, dict]:
    captured = next((row for row in run["snapshot"]["captures"] if row["id"] == capture_id), None)
    scale_row = next((row for row in run["summary"]["captures"] if row["id"] == capture_id), None)
    if captured is None or scale_row is None:
        raise Missing("Choose a capture in this archived reconstruction.")
    return captured, scale_row


def _motion(values: dict) -> Motion:
    return Motion(
        dx=values["dx"], dy=values["dy"], scale=values["scale"],
        similarity=values["score"], overlap=values["overlap"],
    )


def pair_from_run(library, run: dict, first: str, second: str, *, registered: bool) -> Pair:
    if first == second:
        raise Unprocessable("Choose two different captures.")
    original, first_pose = _for_id(run, first)
    other, other_pose = _for_id(run, second)
    summary = run["summary"]
    size = tuple(summary["processed_size"])
    box = AnalysisBox(**summary["crop"])
    lighting = summary.get("lighting")
    first_capture = _matched_capture(library, original, size, capture_gain(lighting, first))
    second_capture = _matched_capture(library, other, size, capture_gain(lighting, second))
    if registered:
        fixed = warp(first_capture, _motion(first_pose["motion"]))
        moved = warp(second_capture, _motion(other_pose["motion"]))
    else:
        fixed = first_capture
        moved = second_capture
    return Pair(
        box.slice(fixed.rgb), box.slice(moved.rgb),
        box.slice(fixed.support), box.slice(moved.support),
        original["name"], other["name"],
        (first_pose["display_position"], other_pose["display_position"]),
        registered,
        summary["scale"]["rail_unit"],
    )


def _matched_capture(library, capture: dict, size: tuple[int, int], gain: float):
    pixels = read_pixels(library, capture, size)
    if abs(gain - 1.0) >= 1e-6:
        pixels.rgb = apply_gain(pixels.rgb, gain)
    return pixels


def _local_contrast(image: np.ndarray) -> np.ndarray:
    small = ndimage.gaussian_filter(image, sigma=0.9, mode="nearest")
    broad = ndimage.gaussian_filter(image, sigma=3.4, mode="nearest")
    return small - broad


def _correlation(first: np.ndarray, second: np.ndarray) -> float | None:
    if first.size < 2:
        return None
    first = first.astype(np.float64)
    second = second.astype(np.float64)
    first -= first.mean()
    second -= second.mean()
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if denominator < 1e-8:
        return None
    return float(np.dot(first, second) / denominator)


def compare_metrics(pair: Pair, selection: Region | None) -> dict:
    height, width = pair.first_support.shape
    if selection:
        x, y, right, bottom = selection.pixels(width, height)
        box = AnalysisBox(x, y, right - x, bottom - y)
    else:
        box = AnalysisBox(0, 0, width, height)
    first = linear_luminance(box.slice(pair.first_rgb))
    second = linear_luminance(box.slice(pair.second_rgb))
    valid = box.slice(pair.shared)
    first_detail = _local_contrast(first)
    second_detail = _local_contrast(second)
    return {
        "first": {"name": pair.first_name, "position": pair.positions[0]},
        "second": {"name": pair.second_name, "position": pair.positions[1]},
        "registered": pair.transformed,
        "region": None if selection is None else selection.public(),
        "bounds": box.public(),
        "unit": pair.unit,
        "coverage": float(valid.mean()),
        "mean_difference": float(np.mean(np.abs(first[valid] - second[valid]))) if valid.any() else None,
        "detail_correlation": _correlation(first_detail[valid], second_detail[valid]),
        "first_luminance": percentile_summary(first, valid),
        "second_luminance": percentile_summary(second, valid),
    }


def _split_image(pair: Pair, divider: float) -> Image.Image:
    width = pair.first_support.shape[1]
    threshold = int(np.clip(round(width * divider), 1, width - 1))
    color = pair.first_rgb.copy()
    support = pair.first_support.copy()
    color[:, threshold:] = pair.second_rgb[:, threshold:]
    support[:, threshold:] = pair.second_support[:, threshold:]
    return covered_image(linear_to_srgb(color), support)


def _difference_image(pair: Pair, gain: float) -> Image.Image:
    difference = np.abs(linear_luminance(pair.first_rgb) - linear_luminance(pair.second_rgb))
    strength = np.clip(difference * gain, 0, 1)
    color = np.empty((*strength.shape, 3), dtype=np.float32)
    color[..., 0] = 0.025 + 0.95 * strength
    color[..., 1] = 0.02 + 0.31 * strength
    color[..., 2] = 0.04 + 0.10 * strength
    return covered_image(linear_to_srgb(color), pair.shared)


def _dual_panel(pair: Pair) -> Image.Image:
    size = fit_size(pair.first_support.shape[::-1], 690)
    first = covered_image(linear_to_srgb(pair.first_rgb), pair.first_support).convert("RGBA")
    second = covered_image(linear_to_srgb(pair.second_rgb), pair.second_support).convert("RGBA")
    first.thumbnail(size)
    second.thumbnail(size)
    sheet = Image.new("RGBA", (first.width + second.width + 10, max(first.height, second.height)), (18, 21, 26, 255))
    sheet.alpha_composite(first, (0, 0))
    sheet.alpha_composite(second, (first.width + 10, 0))
    return sheet


def render_pair(pair: Pair, mode: str, amount: float) -> Image.Image:
    if mode == "split":
        return _split_image(pair, amount)
    if mode == "overlay":
        return difference_overlay(linear_luminance(pair.first_rgb), linear_luminance(pair.second_rgb), pair.shared)
    if mode == "difference":
        return _difference_image(pair, 1 + amount * 9)
    return _dual_panel(pair)
