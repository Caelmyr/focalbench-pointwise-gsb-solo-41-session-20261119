"""Level a tilted focus surface against a robust flat-base reference plane.

The shim can be clamped at a slight angle, so a physically flat bottom reads as
a depth ramp.  A reference rectangle that is mostly flat bottom defines the
tilt: its reliable depth samples fit a plane with an iteratively reweighted
least squares pass, so a raised step touching the edge of the rectangle cannot
drag the datum toward it.  Deviations are measured in the archived rail units
and pixels without a reliable depth stay empty.
"""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .imaging import percentile_summary
from .models import Region, Unprocessable
from .surface import Inspection

MIN_REFERENCE_PIXELS = 120
MIN_REFERENCE_FRACTION = 0.5
PLANE_ITERATIONS = 4
TUKEY_CUTOFF = 4.685
HISTOGRAM_BINS = 18

# Diverging blue (below the plane) -> neutral -> amber (above the plane).
_DIVERGING = np.array([
    [48, 96, 178], [86, 134, 200], [140, 176, 214], [202, 214, 224],
    [236, 232, 222], [238, 214, 168], [226, 178, 110], [202, 138, 74],
], dtype=np.float32)


@dataclass(frozen=True)
class LevelPlane:
    intercept: float
    slope_x: float
    slope_y: float
    inliers: int
    reliable: int
    weights: np.ndarray
    xs: np.ndarray
    ys: np.ndarray
    depths: np.ndarray

    def predict(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        return self.intercept + self.slope_x * xs + self.slope_y * ys


@dataclass(frozen=True)
class Leveling:
    plane: LevelPlane
    deviation: np.ndarray
    reliable: np.ndarray
    report: dict


def _region_arrays(inspection: Inspection, region: Region):
    left, top, right, bottom = region.pixels(inspection.frame.width, inspection.frame.height)
    viewport = slice(top, bottom), slice(left, right)
    depth = np.asarray(inspection.depth[viewport], dtype=np.float64)
    reliable = np.asarray(inspection.reliable[viewport], dtype=bool) & np.isfinite(depth)
    return left, top, right, bottom, depth, reliable


def robust_plane(inspection: Inspection, region: Region) -> LevelPlane:
    """Fit z = a + bx + cy, rejecting raised/lowered patches with Tukey weights."""
    _, _, _, _, depth, reliable = _region_arrays(inspection, region)
    count = int(reliable.sum())
    if count < MIN_REFERENCE_PIXELS or count / reliable.size < MIN_REFERENCE_FRACTION:
        raise Unprocessable(
            "The reference patch needs a larger, well-focused flat area "
            f"(at least {MIN_REFERENCE_PIXELS} reliable pixels and "
            f"{MIN_REFERENCE_FRACTION:.0%} coverage)."
        )
    ys, xs = np.nonzero(reliable)
    zs = depth[reliable]
    design = np.column_stack((np.ones_like(xs, dtype=np.float64), xs, ys))
    beta, *_ = np.linalg.lstsq(design, zs, rcond=None)
    weights = np.ones(count, dtype=np.float64)
    for _ in range(PLANE_ITERATIONS):
        residual = zs - design @ beta
        centered = residual - np.median(residual)
        scale = float(np.median(np.abs(centered)))
        if scale <= 1e-12:
            # Residuals are effectively identical: every sample is an inlier.
            weights = np.ones(count, dtype=np.float64)
            break
        standardized = residual / (TUKEY_CUTOFF * scale)
        proposal = np.where(
            np.abs(standardized) < 1.0,
            (1.0 - standardized ** 2) ** 2,
            0.0,
        )
        # A genuine flat datum keeps most of the patch; a collapsing weight set
        # means the scale estimate became unstable, so keep the earlier fit.
        if proposal.sum() < max(6.0, 0.5 * count):
            break
        weights = proposal
        weighted = design * weights[:, None]
        gram = weighted.T @ design
        try:
            next_beta = np.linalg.solve(gram, weighted.T @ zs)
        except np.linalg.LinAlgError:
            break
        if np.allclose(next_beta, beta, rtol=1e-10, atol=1e-12):
            beta = next_beta
            break
        beta = next_beta
    inliers = int(np.count_nonzero(weights >= 0.1))
    return LevelPlane(
        intercept=float(beta[0]), slope_x=float(beta[1]), slope_y=float(beta[2]),
        inliers=max(inliers, min(count, 6)), reliable=count,
        weights=weights, xs=xs, ys=ys, depths=zs,
    )


def _deviation_histogram(values: np.ndarray, bins: int) -> dict:
    edges = np.linspace(float(values.min()), float(values.max()), bins + 1)
    counts, _ = np.histogram(values, bins=edges)
    return {"edges": edges.tolist(), "counts": counts.tolist()}


def level_surface(inspection: Inspection, reference: Region, target: Region) -> Leveling:
    plane = robust_plane(inspection, reference)
    height, width = inspection.depth.shape
    grid_y, grid_x = np.mgrid[0:height, 0:width]
    deviation = np.asarray(inspection.depth, dtype=np.float64) - plane.predict(grid_x, grid_y)
    reliable = np.asarray(inspection.reliable, dtype=bool) & np.isfinite(deviation)

    left, top, right, bottom, _, target_reliable = _region_arrays(inspection, target)
    local = deviation[top:bottom, left:right][target_reliable]
    measured = int(local.size)
    stats = percentile_summary(deviation[top:bottom, left:right], target_reliable)
    histogram = _deviation_histogram(local, HISTOGRAM_BINS) if measured else {
        "edges": [], "counts": [],
    }
    if measured:
        limit = float(max(abs(np.percentile(local, 2)), abs(np.percentile(local, 98)),
                          abs(float(stats["median"]))))
    else:
        limit = 0.0

    reference_residual = plane.depths - plane.predict(plane.xs, plane.ys)
    ref_stats = percentile_summary(reference_residual, np.ones_like(reference_residual, dtype=bool))
    ref_spread = float(reference_residual.max() - reference_residual.min())
    robust_spread = float(ref_stats["p95"] - ref_stats["p05"])
    rejected = int(np.count_nonzero(plane.weights < 0.1))
    rejected_fraction = rejected / max(1, plane.reliable)
    warnings = []
    if rejected_fraction >= 0.02:
        warnings.append(
            f"{rejected} reference pixels ({rejected_fraction:.1%}) sit off the fitted plane, "
            "likely a raised step or edge; they were down-weighted."
        )
    rail_span = max(inspection.positions[-1] - inspection.positions[0], 1e-12)
    if robust_spread > rail_span * 0.25:
        warnings.append(
            "The reference patch spans a large focal-depth range. "
            "Choose a flatter bottom area for a more repeatable datum."
        )
    if measured == 0:
        warnings.append("The inspection area has no reliable depth; its readings are empty.")
    elif measured / max(1, target_reliable.size) < 0.5:
        warnings.append("Much of the inspection area lacks reliable depth; blank pixels are excluded.")
    report = {
        "unit": inspection.unit,
        "warnings": warnings,
        "reference": {
            "region": reference.public(),
            "reliable_pixels": plane.reliable,
            "inlier_pixels": plane.inliers,
            "rejected_pixels": rejected,
            "rejected_fraction": rejected_fraction,
            "slope_x": plane.slope_x,
            "slope_y": plane.slope_y,
            "residual_median": ref_stats["median"],
            "residual_range": ref_spread,
            "residual_p05_p95": robust_spread,
        },
        "inspection": {
            "region": target.public(),
            "reliable_pixels": measured,
            "reliable_fraction": measured / max(1, target_reliable.size),
            "median_deviation": stats["median"],
            "minimum_deviation": stats["min"],
            "maximum_deviation": stats["max"],
            "deviation_range": None if not measured else stats["max"] - stats["min"],
            "p05": stats["p05"],
            "p95": stats["p95"],
            "mean": stats["mean"],
            "histogram": histogram,
        },
        "color_limit": limit,
    }
    return Leveling(plane, deviation, reliable, report)


def _diverging_color(fraction: np.ndarray) -> np.ndarray:
    """Map a [-1, 1] array to the blue-neutral-amber palette."""
    value = np.clip(fraction, -1.0, 1.0)
    position = (value + 1.0) * 0.5 * (len(_DIVERGING) - 1)
    before = np.floor(position).astype(np.int32)
    after = np.minimum(before + 1, len(_DIVERGING) - 1)
    amount = (position - before)[..., None]
    return _DIVERGING[before] * (1.0 - amount) + _DIVERGING[after] * amount


def level_image(inspection: Inspection, leveling: Leveling) -> Image.Image:
    reference = Region(**leveling.report["reference"]["region"])
    target = Region(**leveling.report["inspection"]["region"])
    deviation, reliable = leveling.deviation, leveling.reliable
    height, width = deviation.shape

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    limit = max(float(leveling.report["color_limit"]), 1e-12)
    colors = _diverging_color(deviation / limit)
    rgba[reliable, :3] = np.clip(colors[reliable], 0, 255).astype(np.uint8)
    rgba[reliable, 3] = 255

    legend_h = 34
    canvas = Image.new("RGBA", (width, height + legend_h), (16, 19, 24, 255))
    map_layer = Image.fromarray(rgba, mode="RGBA")
    canvas.paste(map_layer, (0, 0))
    draw = ImageDraw.Draw(canvas)
    face = ImageFont.load_default(size=13)
    strip_y0, strip_y1 = height + 8, height + 20
    strip_x0, strip_x1 = 10, min(width - 10, 250)
    steps = strip_x1 - strip_x0
    ramp = _diverging_color(np.linspace(-1.0, 1.0, steps)).astype(np.uint8)
    for index, color in enumerate(ramp):
        draw.line((strip_x0 + index, strip_y0, strip_x0 + index, strip_y1),
                  fill=(int(color[0]), int(color[1]), int(color[2]), 255))
    unit = leveling.report["unit"].replace("µm", "um").replace("µ", "u")
    draw.text((strip_x1 + 10, strip_y0),
              f"below -{limit:.3g} {unit}   0   above +{limit:.3g} {unit}   (blank = no reliable depth)",
              fill=(210, 218, 224, 255), font=face)

    def outline(region: Region, color: tuple[int, int, int], label: str) -> None:
        left, top, right, bottom = region.pixels(width, height)
        draw.rectangle((left, top, max(left, right - 1), max(top, bottom - 1)),
                       outline=color, width=2)
        label_w = int(draw.textlength(label, font=face)) + 10
        label_top = max(0, top - 17)
        draw.rectangle((left, label_top, left + label_w, label_top + 16), fill=(18, 21, 27, 235))
        draw.text((left + 5, label_top + 1), label, fill=color, font=face)

    outline(reference, (255, 199, 107), "Reference plane")
    outline(target, (137, 229, 234), "Inspection area")
    return canvas
