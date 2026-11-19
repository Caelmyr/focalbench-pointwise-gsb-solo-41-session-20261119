"""Supported, linear-light fusion and visual result layers."""

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .focus import FocusEvidence
from .imaging import linear_to_srgb

_PALETTE = np.array([
    [20, 24, 53], [42, 45, 112], [50, 80, 155], [42, 123, 157],
    [37, 160, 140], [86, 190, 111], [167, 208, 98], [247, 224, 148],
], dtype=np.float32)
_CONFIDENCE_PALETTE = np.array([
    [20, 24, 30], [73, 65, 95], [142, 112, 153], [238, 222, 183],
], dtype=np.float32)
_PLANE_COLORS = np.array([
    [114, 131, 235], [117, 174, 235], [102, 209, 219], [96, 215, 173],
    [104, 202, 122], [164, 205, 112], [212, 206, 114], [238, 185, 117],
    [225, 132, 122], [214, 104, 170], [196, 134, 224], [184, 160, 242],
], dtype=np.float32)

EPSILON = 1.0e-7


@dataclass
class ProductImages:
    composite: Image.Image
    depth: Image.Image
    confidence: Image.Image
    coverage: Image.Image
    plane: Image.Image
    overlap: Image.Image
    depth16: Image.Image


def _choose_rgb(rgb: np.ndarray, selected: np.ndarray) -> np.ndarray:
    indexes = selected[None, ..., None]
    return np.take_along_axis(rgb, indexes, axis=0)[0]


def confidence_weights(
    scores: np.ndarray,
    support: np.ndarray,
    evidence: FocusEvidence,
    strength: float,
) -> np.ndarray:
    relative = scores / np.maximum(evidence.response_at_peak[None, ...], EPSILON)
    exponent = 2.5 + float(strength) * 8.0
    relative = np.clip(relative, 0, 1) ** exponent
    relative *= support
    minimum = np.maximum(0.28, 0.68 - evidence.confidence * 0.34)
    relative[scores < evidence.response_at_peak[None, ...] * minimum[None, ...]] = 0
    rows, cols = np.indices(evidence.selected.shape)
    relative[evidence.selected, rows, cols] += evidence.valid * (0.25 + strength * 0.25)
    return relative.astype(np.float32, copy=False)


def _normalize(weights: np.ndarray, supports: np.ndarray) -> np.ndarray:
    weights *= supports
    total = weights.sum(axis=0, dtype=np.float32)
    np.divide(weights, np.maximum(total, EPSILON)[None, ...], out=weights)
    return weights


def _blend_rgb(rgb: np.ndarray, weight: np.ndarray) -> np.ndarray:
    value = np.zeros(rgb.shape[1:], dtype=np.float32)
    for color, alpha in zip(rgb, weight):
        value += color * alpha[..., None]
    return value


def feather_blend(
    rgb: np.ndarray,
    support: np.ndarray,
    weights: np.ndarray,
    strength: float,
) -> np.ndarray:
    feather = 0.7 + (1 - strength) * 2.6
    for index in range(weights.shape[0]):
        normal = ndimage.gaussian_filter(support[index].astype(np.float32), feather, mode="nearest")
        blurred = ndimage.gaussian_filter(weights[index], feather, mode="nearest")
        weights[index] = blurred / np.maximum(normal, EPSILON)
    return _blend_rgb(rgb, _normalize(weights, support))


def _filtered_rgb(array: np.ndarray, support: np.ndarray, sigma: float) -> np.ndarray:
    coverage = ndimage.gaussian_filter(support.astype(np.float32), sigma=sigma, mode="nearest")
    output = np.empty_like(array)
    for band in range(array.shape[2]):
        premultiplied = np.where(support, array[..., band], 0)
        filtered = ndimage.gaussian_filter(premultiplied, sigma=sigma, mode="nearest")
        output[..., band] = filtered / np.maximum(coverage, EPSILON)
    return output


def _blurred_weights(weights: np.ndarray, support: np.ndarray, sigma: float) -> np.ndarray:
    result = np.empty_like(weights)
    for index in range(len(weights)):
        coarse = ndimage.gaussian_filter(weights[index], sigma, mode="nearest")
        masks = ndimage.gaussian_filter(support[index].astype(np.float32), sigma, mode="nearest")
        result[index] = coarse / np.maximum(masks, EPSILON)
    return _normalize(result, support)


def pyramid_blend(
    rgb: np.ndarray,
    support: np.ndarray,
    weights: np.ndarray,
    strength: float,
) -> np.ndarray:
    output = np.zeros(rgb.shape[1:], dtype=np.float32)
    first_sigma = 0.8 + 0.8 * (1 - strength)
    second_sigma = first_sigma * 4.2
    fine_weights = _blurred_weights(weights, support, first_sigma * 0.6)
    medium_weights = _blurred_weights(weights, support, first_sigma * 2.2)
    coarse_weights = _blurred_weights(weights, support, second_sigma * 1.3)
    for index, capture in enumerate(rgb):
        fine = _filtered_rgb(capture, support[index], first_sigma)
        coarse = _filtered_rgb(capture, support[index], second_sigma)
        output += (capture - fine) * fine_weights[index, ..., None]
        output += (fine - coarse) * medium_weights[index, ..., None]
        output += coarse * coarse_weights[index, ..., None]
    return output


def assemble_composite(
    rgb: np.ndarray,
    support: np.ndarray,
    scores: np.ndarray,
    evidence: FocusEvidence,
    mode: str,
    strength: float,
) -> np.ndarray:
    if mode == "winner":
        result = _choose_rgb(rgb, evidence.selected)
    else:
        weights = confidence_weights(scores, support, evidence, strength)
        if mode == "feather":
            result = feather_blend(rgb, support, weights, strength)
        else:
            result = pyramid_blend(rgb, support, weights, strength)
    result[~evidence.valid] = 0
    return np.clip(result, 0, 1)


def palette_map(fraction: np.ndarray, palette: np.ndarray) -> np.ndarray:
    value = np.clip(np.nan_to_num(fraction, nan=0.0), 0, 1) * (len(palette) - 1)
    before = np.floor(value).astype(np.int32)
    after = np.minimum(before + 1, len(palette) - 1)
    amount = (value - before)[..., None]
    return (palette[before] * (1 - amount) + palette[after] * amount + 0.5).astype(np.uint8)


def covered_image(rgb: np.ndarray, support: np.ndarray) -> Image.Image:
    if support.all():
        return Image.fromarray(rgb)
    rgba = np.empty((*support.shape, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = support.astype(np.uint8) * 255
    return Image.fromarray(rgba)


def depth_fraction(depth: np.ndarray, first: float, last: float) -> np.ndarray:
    return (depth - first) / max(last - first, 1.0e-12)


def output_images(
    composite: np.ndarray,
    evidence: FocusEvidence,
    aligned_support: np.ndarray,
    depth_bounds: tuple[float, float],
) -> ProductImages:
    first, last = depth_bounds
    fraction = depth_fraction(evidence.depth, first, last)
    reliable = evidence.reliable
    shaded = palette_map(fraction, _PALETTE)
    shaded[evidence.valid & ~reliable] = [36, 39, 47]
    coverage = np.zeros((*evidence.valid.shape, 3), dtype=np.uint8)
    coverage[evidence.valid] = [176, 93, 77]
    coverage[reliable] = [104, 192, 164]
    confidence = palette_map(evidence.confidence, _CONFIDENCE_PALETTE)
    planes = palette_map(
        evidence.selected / max(aligned_support.shape[0] - 1, 1), _PLANE_COLORS,
    )
    planes[evidence.valid & ~reliable] = [37, 40, 50]
    contributor_count = aligned_support.sum(axis=0)
    overlap = palette_map(contributor_count / len(aligned_support), _CONFIDENCE_PALETTE)
    codes = np.zeros(evidence.valid.shape, dtype=np.uint16)
    measured = reliable & np.isfinite(evidence.depth)
    codes[measured] = (np.clip(fraction[measured], 0, 1) * 65534 + 1.5).astype(np.uint16)
    return ProductImages(
        composite=covered_image(linear_to_srgb(composite), evidence.valid),
        depth=covered_image(shaded, evidence.valid),
        confidence=covered_image(confidence, evidence.valid),
        coverage=Image.fromarray(coverage),
        plane=covered_image(planes, evidence.valid),
        overlap=covered_image(overlap, evidence.valid),
        depth16=Image.fromarray(codes),
    )


def difference_overlay(left: np.ndarray, right: np.ndarray, support: np.ndarray) -> Image.Image:
    green = np.clip(left * 0.95 + right * 0.05, 0, 1)
    magenta = np.clip(right * 0.95 + left * 0.05, 0, 1)
    cyan_magenta = np.stack([magenta, green, magenta], axis=2)
    indices = ~support
    cyan_magenta[indices] = np.array([0.06, 0.06, 0.08])
    return covered_image(linear_to_srgb(cyan_magenta), support)
