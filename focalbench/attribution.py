"""Inspect where a recorded focus plane contributed to an archived result."""

import hashlib
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .alignment import AnalysisBox, Motion, warp
from .imaging import linear_to_srgb, read_pixels
from .models import Missing
from .surface import Inspection


@dataclass
class Attribution:
    capture: dict
    row: dict
    selected: np.ndarray
    eligible: np.ndarray
    supported: np.ndarray
    reliable: np.ndarray


def _entry(result: dict, frame_id: str) -> tuple[dict, dict]:
    row = next((r for r in result["summary"]["captures"] if r["id"] == frame_id), None)
    frame = next((r for r in result["snapshot"]["captures"] if r["id"] == frame_id), None)
    if row is None or frame is None:
        raise Missing("This focus plane was not included in the result.")
    return frame, row


def create_attribution(inspection: Inspection, result: dict, frame_id: str) -> Attribution:
    frame, row = _entry(result, frame_id)
    selected = inspection.winner == row["index"]
    evidence = inspection._layer("response-support")[row["index"]]
    return Attribution(
        frame, row, selected & inspection.valid, evidence, inspection.valid,
        selected & inspection.reliable,
    )


def _pixel_area(selection: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    groups, count = ndimage.label(selection, structure=ndimage.generate_binary_structure(2, 2))
    boxes = ndimage.find_objects(groups, max_label=count)
    sizes = np.bincount(groups.ravel(), minlength=count + 1)
    return groups, sizes, boxes


def focus_islands(attribution: Attribution, minimum_size: int, limit: int = 12) -> dict:
    _, sizes, boxes = _pixel_area(attribution.reliable)
    ranks = sorted(range(1, len(sizes)), key=lambda n: (-sizes[n], n))
    by_size = [number for number in ranks if sizes[number] >= minimum_size]
    islands = []
    for region_number in by_size[:limit]:
        y, x = boxes[region_number - 1]
        islands.append({
            "rank": len(islands) + 1,
            "pixels": int(sizes[region_number]),
            "x": int(x.start),
            "y": int(y.start),
            "width": int(x.stop - x.start),
            "height": int(y.stop - y.start),
        })
    return {
        "minimum_pixels": minimum_size,
        "distinct_islands": len(by_size),
        "returned_islands": islands,
        "island_pixels": int(sizes[by_size].sum()) if by_size else 0,
    }


def contribution_summary(inspection: Inspection, attribution: Attribution, minimum_size: int) -> dict:
    evidence = inspection._layer("response")[attribution.row["index"]]
    accepted_scores = evidence[attribution.reliable]
    area = int(attribution.selected.size)
    area_scale = inspection.frame.pixel_area_um2
    reliable_count = int(attribution.reliable.sum())
    return {
        "capture": {
            "id": attribution.capture["id"],
            "name": attribution.capture["name"],
            "position": attribution.row.get("display_position"),
            "unit": inspection.unit,
        },
        "eligible_pixels": int(attribution.eligible.sum()),
        "winner_pixels": int(attribution.selected.sum()),
        "reliable_winner_pixels": reliable_count,
        "selected_fraction": float(attribution.selected.mean()),
        "reliable_fraction": reliable_count / area,
        "reliable_given_selected": reliable_count / max(1, int(attribution.selected.sum())),
        "source_image_area_um2": None if area_scale is None else area_scale * reliable_count,
        "reliable_mean_response": float(accepted_scores.mean()) if accepted_scores.size else None,
        **focus_islands(attribution, minimum_size),
    }


def _registered_source(library, attribution: Attribution, result: dict):
    summary = result["summary"]
    pixels = read_pixels(library, attribution.capture, tuple(summary["processed_size"]))
    metadata = attribution.row["motion"]
    aligned = warp(pixels, Motion(dx=metadata["dx"], dy=metadata["dy"], scale=metadata["scale"]))
    box = AnalysisBox(**summary["crop"])
    return box.slice(aligned.rgb), box.slice(aligned.support)


def contribution_image(library, result: dict, attribution: Attribution, view: str):
    source, source_support = _registered_source(library, attribution, result)
    sample = linear_to_srgb(source)
    if view == "registered":
        rgba = np.concatenate((sample, source_support[..., None].astype(np.uint8) * 255), axis=2)
        return Image.fromarray(rgba)
    if view == "mask":
        code = np.full(source_support.shape, 32, dtype=np.uint8)
        code[attribution.eligible] = 84
        code[attribution.selected] = 145
        code[attribution.reliable] = 255
        code[~attribution.supported] = 0
        return Image.fromarray(code)
    highlighted = sample.astype(np.float32) * 0.33
    highlighted[attribution.reliable] = (
        sample[attribution.reliable] * 0.75 + np.array([73, 232, 189]) * 0.25
    )
    edges = attribution.reliable & ~ndimage.binary_erosion(attribution.reliable, iterations=2)
    highlighted[edges] = (107, 237, 191)
    highlighted[~source_support] = (19, 23, 33)
    return Image.fromarray(highlighted.astype(np.uint8))


def run_image_integrity(library, result: dict) -> dict:
    input_checks = []
    for capture in result["snapshot"]["captures"]:
        source = library.source_path(capture["sha"])
        if source.is_file():
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            state = "matched" if digest == capture["sha"] else "different"
        else:
            digest = None
            state = "missing"
        input_checks.append({
            "capture_id": capture["id"], "name": capture["name"],
            "expected_sha256": capture["sha"], "actual_sha256": digest,
            "state": state,
        })
    return {
        "all_sources_match": all(item["state"] == "matched" for item in input_checks),
        "checks": input_checks,
    }
