"""Export an image patch and its exact focus evidence without resizing or filling gaps."""

import io
import math
import zipfile
from dataclasses import dataclass

import numpy as np
from PIL import Image, PngImagePlugin

from .models import RUN_ASSETS, Capacity, Region
from .reports import add_zip_data, csv_bytes, json_bytes
from .surface import Inspection

MAX_CSV_PIXELS = 32_000


@dataclass(frozen=True)
class Patch:
    left: int
    top: int
    right: int
    bottom: int
    normalized: Region

    @classmethod
    def choose(cls, inspection: Inspection, area: Region):
        edges = area.pixels(inspection.frame.width, inspection.frame.height)
        return cls(*edges, area)

    @property
    def rectangle(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    @property
    def vertical(self) -> slice:
        return slice(self.top, self.bottom)

    @property
    def horizontal(self) -> slice:
        return slice(self.left, self.right)

    @property
    def pixel_count(self) -> int:
        return (self.right - self.left) * (self.bottom - self.top)

    def canvas_values(self, layer: np.ndarray):
        return layer[self.vertical, self.horizontal]


def _patch_image_bytes(source, patch: Patch, metadata: dict) -> bytes:
    with Image.open(source) as image:
        tile = image.crop(patch.rectangle)
    info = PngImagePlugin.PngInfo()
    info.add_text("FocalBench", "Archived native-resolution inspection patch")
    info.add_text("Run", metadata["result_id"])
    info.add_text("Reference", metadata["reference_id"])
    info.add_text("OriginX", str(patch.left))
    info.add_text("OriginY", str(patch.top))
    output = io.BytesIO()
    tile.save(output, format="PNG", pnginfo=info, compress_level=4)
    return output.getvalue()


def _patch_depth_file(inspection: Inspection, patch: Patch) -> bytes:
    depth = patch.canvas_values(inspection.depth).astype(np.float32)
    reliable = patch.canvas_values(inspection.reliable)
    depth[~reliable] = np.nan
    output = io.BytesIO()
    tile = Image.fromarray(depth)
    tile.save(output, format="TIFF", compression="tiff_adobe_deflate")
    return output.getvalue()


def _patch_csv(inspection: Inspection, patch: Patch) -> bytes:
    if patch.pixel_count > MAX_CSV_PIXELS:
        raise Capacity(f"Select a patch of up to {MAX_CSV_PIXELS:,} pixels for a sample CSV.")
    ids = [capture["id"] for capture in inspection.summary["captures"]]
    samples = []
    for y in range(patch.top, patch.bottom):
        for x in range(patch.left, patch.right):
            visible = bool(inspection.valid[y, x])
            reliable = bool(inspection.reliable[y, x])
            original_x, original_y = inspection.frame.reference_point(inspection.frame.center(x, y))
            index = int(inspection.winner[y, x])
            samples.append((
                x, y, original_x, original_y, visible, reliable,
                float(inspection.depth[y, x]) if reliable else None,
                float(inspection.confidence[y, x]) if visible else None,
                ids[index] if visible else None,
                inspection.plane_value(index) if visible else None,
            ))
    return csv_bytes(
        ("output_x", "output_y", "original_center_x", "original_center_y", "visible", "reliable",
         "estimated_depth", "confidence", "selected_capture", "selected_position"),
        samples,
    )


def _patch_orientation(inspection: Inspection, patch: Patch) -> dict:
    frame = inspection.frame
    relative_corners = [
        (patch.left / frame.width, patch.top / frame.height),
        (patch.right / frame.width, patch.bottom / frame.height),
    ]
    original_edges = [frame.reference_point(point) for point in relative_corners]
    return {
        "pixel_center_reference": "original image edges start at 0; pixel centers start at 0.5",
        "output_box": list(patch.rectangle),
        "original_edge_bounds": list((*original_edges[0], *original_edges[1])),
        "normalized_selection": patch.normalized.public(),
        "pixel_area_um2": frame.pixel_area_um2,
        "height": patch.bottom - patch.top,
        "width": patch.right - patch.left,
    }


def metadata_for_patch(inspection: Inspection, result: dict, patch: Patch) -> dict:
    mapped = _patch_orientation(inspection, patch)
    count = patch.pixel_count
    data_present = int(patch.canvas_values(inspection.valid).sum())
    confident = int(patch.canvas_values(inspection.reliable).sum())
    encoding = result["summary"]["depth_encoding"]
    return {
        "schema": "focalbench/inspection-patch-1",
        "result_id": result["id"],
        "result_title": result["title"],
        "result_created_at": result["created_at"],
        "reference_id": result["summary"]["reference_id"],
        "reference_name": result["summary"]["reference_name"],
        "position_unit": inspection.unit,
        "coverage": {"total": count, "visible": data_present, "reliable": confident},
        "depth_tiff": {"samples": "float32", "nodata": "NaN", "rail_origin_already_applied": True},
        "depth_png": {
            "near": encoding["near"], "far": encoding["far"],
            "no_data": encoding["no_data"], "minimum_code": 1, "maximum_code": 65535,
            "unit": encoding["unit"], "calibrated": encoding.get("calibrated", False),
        },
        **mapped,
    }


def make_patch(inspection: Inspection, result: dict, area: Region, include_csv: bool) -> io.BytesIO:
    patch = Patch.choose(inspection, area)
    if include_csv and patch.pixel_count > MAX_CSV_PIXELS:
        raise Capacity(f"Select a patch of up to {MAX_CSV_PIXELS:,} pixels for a sample CSV.")
    metadata = metadata_for_patch(inspection, result, patch)
    result_bytes = io.BytesIO()
    with zipfile.ZipFile(result_bytes, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
        for key in ("composite", "depth", "depth16", "confidence", "coverage"):
            filename = RUN_ASSETS[key]
            image = _patch_image_bytes(inspection.directory / filename, patch, metadata)
            add_zip_data(archive, f"patch/{filename}", image)
        add_zip_data(archive, "patch/positions.tiff", _patch_depth_file(inspection, patch))
        add_zip_data(archive, "patch.json", json_bytes(metadata))
        if include_csv:
            add_zip_data(archive, "pixels.csv", _patch_csv(inspection, patch))
    result_bytes.seek(0, io.SEEK_SET)
    return result_bytes


def possible_patch_outputs(inspection: Inspection, region: Region) -> dict:
    patch = Patch.choose(inspection, region)
    total = patch.pixel_count
    pixel_size = inspection.frame.pixel_area_um2
    return {
        "pixel_bounds": patch.rectangle,
        "pixels": total,
        "square_um": None if pixel_size is None else pixel_size * total,
        "estimated_float_bytes": total * np.dtype(np.float32).itemsize,
        "pixel_csv_allowed": total <= MAX_CSV_PIXELS,
        "pixel_csv_limit": MAX_CSV_PIXELS,
    }
