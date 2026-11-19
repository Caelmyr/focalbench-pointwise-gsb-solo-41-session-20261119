"""Image proofs and report data from a single, frozen reconstruction."""

import csv
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import RUN_ASSETS
from .surface import Inspection

INK = (34, 43, 54)
SOFT = (83, 98, 113)
PAPER = (240, 242, 240)
PAGE_DARK = (24, 29, 35)
BLUE = (62, 100, 185)
STAMP = (190, 211, 237)
PROOF_IMAGES = ("composite", "depth", "confidence", "overlap", "coverage", "plane", "depth16")
ANNOTATION_COLORS = {
    "amber": (253, 199, 94, 255),
    "cyan": (100, 228, 235, 255),
    "magenta": (238, 168, 227, 255),
    "white": (255, 255, 255, 255),
}


def clean_filename(value: str, default: str = "stack") -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-_").lower()
    return (stem[:72] or default).strip("-_")


def _font(size: int):
    return ImageFont.load_default(size=size)


def _format(value, precision=3) -> str:
    return "—" if value is None else f"{value:.{precision}f}".rstrip("0").rstrip(".")


def _short(value: str, length: int) -> str:
    value = value.replace("\n", " ").strip()
    return value if len(value) <= length else value[:length - 1] + "…"


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def spreadsheet_value(value):
    if isinstance(value, str) and value.lstrip("\t\r\n ").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_bytes(header: tuple, records: list) -> bytes:
    dest = io.StringIO()
    writer = csv.writer(dest, lineterminator="\n")
    writer.writerow(header)
    for row in records:
        writer.writerow(spreadsheet_value(value) for value in row)
    return dest.getvalue().encode("utf-8-sig")


def freeze_zip_meta(source: zipfile.ZipFile, name: str):
    info = zipfile.ZipInfo(filename=name)
    info.compress_type = source.compression
    info.external_attr = 0o644 << 16
    return info


def add_zip_data(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    archive.writestr(freeze_zip_meta(archive, name), data)


def _capture_rows(summary: dict):
    for item in summary["captures"]:
        motion = item["motion"]
        light = item.get("light") or {}
        yield (
            item["index"] + 1, item["slot"], item["id"], item["name"], item["position"],
            item["display_position"], summary["scale"]["rail_unit"], item["is_reference"],
            motion["dx"], motion["dy"], motion["scale"], motion["score"], motion["overlap"],
            light.get("status"), light.get("gain"), light.get("log_sigma"), light.get("hops"),
            light.get("reason"),
            item["valid_fraction"], item["median_response"], item["p95_response"],
            item["winner_count"], item["winner_fraction"], motion["warning"],
        )


def capture_report(summary: dict) -> bytes:
    fields = (
        "plane", "capture_slot", "capture_id", "capture_filename", "rail_position", "display_position",
        "rail_unit", "reference", "shift_x_px", "shift_y_px", "zoom_scale", "alignment_score", "overlap",
        "light_match", "light_gain", "light_log_sigma", "light_hops", "light_note",
        "usable_focus_fraction", "median_response", "p95_response", "reliable_winners",
        "reliable_fraction", "warning",
    )
    return csv_bytes(fields, list(_capture_rows(summary)))


def profile_report(section: dict) -> bytes:
    header = (
        "sample", "source_x", "source_y", "position", "rail_unit", "distance_px", "distance_um",
        "reliable", "interpolated", "confidence", "capture_id", "contributing_captures",
    )
    data = []
    for index, sample in enumerate(section["samples"], 1):
        data.append((
            index, sample["source_x"], sample["source_y"], sample["position"], section["unit"],
            sample["distance"], sample["distance_um"], sample["reliable"], sample["interpolated"],
            sample["confidence"], sample["capture_id"], sample["support"],
        ))
    return csv_bytes(header, data)


def schedule_report(results: dict, unit: str) -> bytes:
    details = results["matches"]["targets"]
    header = ("capture_order", "target_rail_position", "rail_unit", "recorded_position", "offset", "capture_id")
    return csv_bytes(header, [
        (pair["index"], pair["target"], unit, pair["recorded"], pair["offset"], pair["capture_id"])
        for pair in details
    ])


def annotations_report(marks: list[dict], title: str, unit: str) -> dict:
    return {"analysis": title, "position_unit": unit, "space": "normalized archived result", "marks": marks}


def _source_count(summary: dict) -> str:
    fused = summary.get("fused_capture_count", summary["capture_count"])
    if fused == summary["capture_count"]:
        return f"{fused} source captures"
    return f"{fused} of {summary['capture_count']} source captures"

def inspection_sheet(library, captures: list[dict], title: str, unit: str, columns: int) -> Image.Image:
    width = 338
    photo_size = (308, 200)
    height = 260
    count = len(captures)
    rows = max(1, (count + columns - 1) // columns)
    sheet = Image.new("RGB", (columns * width + 48, rows * height + 110), PAPER)
    draw = ImageDraw.Draw(sheet)
    draw.text((24, 22), _short(title, 75), font=_font(31), fill=INK)
    draw.text((25, 67), f"CAPTURE PROOF   ·   {count} frames   ·   Focus position ({unit})",
              font=_font(17), fill=SOFT)
    for index, capture in enumerate(captures):
        row, col = divmod(index, columns)
        x, y = 24 + col * width, 109 + row * height
        tile = (x, y, x + photo_size[0], y + photo_size[1])
        draw.rounded_rectangle(tile, radius=10, fill=PAGE_DARK)
        with Image.open(library.preview_path(capture["sha"])) as raw:
            frame = raw.convert("RGBA")
            frame.thumbnail(photo_size, Image.Resampling.LANCZOS)
            paste_x = x + (photo_size[0] - frame.width) // 2
            paste_y = y + (photo_size[1] - frame.height) // 2
            sheet.paste(frame, (paste_x, paste_y), frame)
        color = INK if capture["included"] else SOFT
        draw.text((x, y + 207), f"{capture['slot']:02d}  {_short(capture['name'], 26)}",
                  font=_font(17), fill=color)
        status = "Archived" if capture.get("archived") else "Included" if capture["included"] else "Excluded"
        caption = f"{_format(capture['position'])} {unit}   ·   {status}"
        draw.text((x, y + 231), caption, font=_font(14), fill=SOFT)
    return sheet


def _label(draw, location, text, color, limits, size):
    x, y = location
    face = _font(size)
    _, _, right, bottom = draw.textbbox((0, 0), text, font=face)
    x = max(2, min(x + 6, limits[0] - right - 10))
    y = max(2, min(y - size - 3, limits[1] - bottom - 10))
    draw.rounded_rectangle((x - 3, y - 2, x + right + 4, y + bottom + 4),
                           radius=4, fill=(17, 23, 30, 230))
    draw.text((x, y), text, fill=color, font=face)


def _annotated_image(library, run: dict, marks: list[dict]) -> Image.Image:
    with Image.open(library.run_directory(run["id"]) / RUN_ASSETS["composite"]) as raw:
        base = raw.convert("RGBA")
    layer = Image.new("RGBA", base.size)
    draw = ImageDraw.Draw(layer)
    radius = max(4, min(base.size) // 120)
    stroke = max(2, min(base.size) // 240)
    for mark in marks:
        color = ANNOTATION_COLORS[mark["color"]]
        points = [
            (round(x * (base.width - 1)), round(y * (base.height - 1)))
            for x, y in mark["vertices"]
        ]
        if mark["kind"] == "area":
            draw.polygon(points, fill=(*color[:3], 51))
            draw.line((*points, points[0]), width=stroke, fill=color, joint="curve")
        elif mark["kind"] == "line":
            draw.line(points, width=stroke, fill=color)
        else:
            x, y = points[0]
            draw.line((x - radius * 2, y, x + radius * 2, y), fill=color, width=1)
            draw.line((x, y - radius * 2, x, y + radius * 2), fill=color, width=1)
        for x, y in points:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=2)
        _label(draw, points[0], _short(mark["label"], 34), color, base.size, 13)
    return Image.alpha_composite(base, layer)


def _image_card(sheet, source, position, bounds, title, description):
    x, y = position
    width, height = bounds
    canvas = ImageDraw.Draw(sheet)
    canvas.rounded_rectangle((x, y, x + width, y + height), radius=12, fill=PAGE_DARK)
    picture = source.convert("RGBA")
    picture.thumbnail(bounds)
    bx = x + (width - picture.width) // 2
    by = y + (height - picture.height) // 2
    sheet.paste(picture, (bx, by), picture)
    canvas.text((x, y + height + 10), title, font=_font(19), fill=INK)
    canvas.text((x, y + height + 36), description, font=_font(14), fill=SOFT)


def report_sheet(library, run: dict, marks: list[dict]) -> Image.Image:
    summary = run["summary"]
    unit = summary["scale"]["rail_unit"]
    if unit == "step":
        unit = "steps"
    title = summary["project_name"]
    depth = summary["depth_statistics"]
    sheet = Image.new("RGB", (1200, 835), PAPER)
    draw = ImageDraw.Draw(sheet)
    draw.rounded_rectangle((26, 21, 1174, 118), radius=16, fill=PAGE_DARK)
    draw.text((51, 36), _short(run["title"], 68), font=_font(34), fill=(235, 241, 243))
    draw.text((53, 81), _short(title, 57), font=_font(16), fill=STAMP)
    label = datetime.fromisoformat(run["created_at"]).astimezone(timezone.utc).strftime("%d %b %Y  %H:%M UTC")
    draw.text((958, 82), label, font=_font(15), fill=STAMP)
    annotated = _annotated_image(library, run, marks)
    base = library.run_directory(run["id"])
    _image_card(sheet, annotated, (35, 145), (695, 451), "All-in-focus reconstruction",
                f"{summary['width']} × {summary['height']} pixels  ·  {_source_count(summary)}")
    with Image.open(base / RUN_ASSETS["depth"]) as depth_image:
        _image_card(sheet, depth_image, (757, 145), (405, 214), "Estimated focus surface",
                    f"Depth span {_format(depth['p05'])} to {_format(depth['p95'])} {unit} (P05–P95)")
    with Image.open(base / RUN_ASSETS["coverage"]) as coverage:
        _image_card(sheet, coverage, (757, 431), (405, 166), "Measured coverage",
                    f"{summary['reliable_fraction'] * 100:.1f}% reliable  ·  {len(marks)} annotations")
    draw.line((37, 671, 1162, 671), fill=(177, 184, 187), width=2)
    draw.text((38, 694), "Reference capture", font=_font(15), fill=SOFT)
    draw.text((38, 721), _short(summary["reference_name"], 30), font=_font(20), fill=INK)
    draw.text((412, 694), "Rail origin", font=_font(15), fill=SOFT)
    draw.text((412, 721), f"{_format(summary['scale']['rail_offset'])} {unit}", font=_font(20), fill=INK)
    draw.text((746, 694), "Original pixel scale", font=_font(15), fill=SOFT)
    value = summary["scale"]["pixel_um"]
    draw.text((746, 721), "Uncalibrated" if value is None else f"{_format(value)} µm / px", font=_font(20), fill=INK)
    draw.text((38, 786), f"FocalBench {summary['algorithm']}          Reconstruction {run['id'][:12]}",
              font=_font(15), fill=BLUE)
    return sheet


def sheet_bytes(sheet: Image.Image) -> bytes:
    output = io.BytesIO()
    sheet.save(output, format="PNG", compress_level=5)
    return output.getvalue()


def _measurement_manifest(run: dict) -> dict:
    encoding = run["summary"]["depth_encoding"]
    return {
        "schema": "focalbench/reconstruction-1",
        "run_id": run["id"],
        "created_at": run["created_at"],
        "title": run["title"],
        "snapshot": run["snapshot"],
        "summary": run["summary"],
        "depth_decode": {
            "value_at_1": encoding["near"],
            "value_at_65535": encoding["far"],
            "nodata": 0,
            "unit": encoding["unit"],
        },
    }


def evidence_package(library, project_id: str, run_id: str):
    run = library.run(project_id, run_id)
    marks = library.list_marks(project_id, run_id)
    result = io.BytesIO()
    path = library.run_directory(run_id)
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
        for asset in PROOF_IMAGES:
            filename = RUN_ASSETS[asset]
            add_zip_data(archive, f"images/{filename}", (path / filename).read_bytes())
        info = _measurement_manifest(run)
        add_zip_data(archive, "reconstruction.json", json_bytes(info))
        add_zip_data(archive, "capture-evidence.csv", capture_report(run["summary"]))
        unit = run["summary"]["scale"]["rail_unit"]
        add_zip_data(archive, "annotations.json", json_bytes(annotations_report(marks, run["title"], unit)))
        add_zip_data(archive, "annotations.csv", annotations_csv(marks, unit))
        cards = library.review_cards(project_id, run_id)
        if cards:
            add_zip_data(archive, "inspection-cards.json", json_bytes(cards))
        add_zip_data(archive, "inspection-proof.png", sheet_bytes(report_sheet(library, run, marks)))
        wrapper = Inspection(path, run["summary"])
        for mark in marks:
            if mark["kind"] != "line":
                continue
            section = wrapper.cross_section(mark["vertices"], 80)
            name = f"{clean_filename(mark['label'], 'section')}-{mark['id'][:8]}.csv"
            add_zip_data(archive, f"sections/{name}", profile_report(section))
    result.seek(0)
    return result


def annotations_csv(marks: list[dict], unit: str) -> bytes:
    rows = []
    for mark in marks:
        reading = mark["measurement"]
        kind = mark["kind"]
        if kind == "pin":
            position = reading["position"]
            low = high = position
            distance = None
            area = None
            reliable = float(reading["reliable"])
        elif kind == "line":
            position = None
            low, high = reading["minimum"], reading["maximum"]
            distance = reading["length_px"]
            area = None
            reliable = reading["reliable_fraction"]
        else:
            position = reading["positions"]["median"]
            low, high = reading["positions"]["min"], reading["positions"]["max"]
            distance = None
            area = reading["reliable_area_um2"]
            reliable = reading["reliable_fraction"]
        rows.append((
            mark["id"], mark["label"], kind, mark["note"], len(mark["vertices"]),
            position, low, high, unit, distance, area, reliable, mark["created_at"],
            pack_vertices(mark["vertices"]),
        ))
    return csv_bytes(
        ("id", "label", "kind", "note", "vertex_count", "depth", "depth_min", "depth_max", "unit",
         "length_original_px", "measured_area_um2", "reliable_fraction", "created_at", "normalized_vertices"),
        rows,
    )


def pack_vertices(vertices: list) -> str:
    return json.dumps(vertices, separators=(",", ":"))
