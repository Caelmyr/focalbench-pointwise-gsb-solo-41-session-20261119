"""Save a reproducible reading of a frozen image and its selected measurement."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import (
    RUN_ASSETS, Region, choice, integer, object_value, parse_points_query, parse_vertex,
    short_text, validate_keys,
)
from .surface import Inspection

CARD_LAYERS = ("composite", "depth", "confidence", "plane", "overlap", "coverage")
CARD_SELECTIONS = ("none", "pin", "area", "section")
CARD_PAGE = (1000, 782)
CARD_BG = (237, 241, 240)
CARD_INK = (37, 44, 61)


def parse_card(payload: dict):
    raw = object_value(payload, "inspection card")
    validate_keys(raw, {"title", "note", "layer", "selection"}, "inspection card")
    title = short_text(raw.get("title"), "card title", maximum=110)
    note = short_text(raw.get("note", ""), "card note", maximum=420, allow_blank=True)
    layer = choice(raw.get("layer", "composite"), "card layer", CARD_LAYERS)
    selected = object_value(raw.get("selection", {"kind": "none"}), "selection")
    kind = choice(selected.get("kind"), "selection kind", CARD_SELECTIONS)
    if kind == "area":
        validate_keys(selected, {"kind", "region", "bins"}, "selection")
        selection = {
            "kind": kind,
            "region": Region.parse(selected.get("region")).public(),
            "bins": integer(selected.get("bins", 18), "bins", 5, 60),
        }
    elif kind == "section":
        validate_keys(selected, {"kind", "points", "samples"}, "selection")
        selection = {
            "kind": kind,
            "points": parse_points_query(selected.get("points")),
            "samples": integer(selected.get("samples", 80), "samples", 16, 300),
        }
    elif kind == "pin":
        validate_keys(selected, {"kind", "point"}, "selection")
        selection = {"kind": kind, "point": parse_vertex(selected.get("point"), "point")}
    else:
        validate_keys(selected, {"kind"}, "selection")
        selection = {"kind": kind}
    return title, note, layer, selection


def measure_card(inspection: Inspection, selection: dict):
    kind = selection["kind"]
    if kind == "pin":
        return inspection.sample(selection["point"])
    if kind == "area":
        region = Region.parse(selection["region"])
        return inspection.area(region, selection["bins"])
    if kind == "section":
        return inspection.cross_section(selection["points"], selection["samples"])
    return None


def create_card(library, project_id: str, result: dict, payload: dict):
    title, note, layer, selection = parse_card(payload)
    inspection = Inspection(library.run_directory(result["id"]), result["summary"])
    readings = measure_card(inspection, selection)
    content = {
        "layer": layer,
        "note": note,
        "selection": selection,
        "measurement": readings,
        "reference_name": inspection.summary["reference_name"],
        "rail_unit": inspection.unit,
        "surface_unit": inspection.surface_unit,
        "source_revision": result["source_revision"],
    }
    return library.add_card(project_id, result["id"], title, content)


def _scaled_xy(point: list, size: tuple[int, int], origin: tuple[int, int]):
    return origin[0] + point[0] * size[0], origin[1] + point[1] * size[1]


def _draw_selection(draw, selection: dict, size: tuple[int, int], origin: tuple[int, int]):
    kind = selection["kind"]
    ink = (249, 206, 102)
    if kind == "pin":
        x, y = _scaled_xy(selection["point"], size, origin)
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), outline=ink, width=3)
        draw.line((x - 12, y, x + 12, y), fill=ink)
        draw.line((x, y - 12, x, y + 12), fill=ink)
    elif kind == "section":
        vertices = [_scaled_xy(point, size, origin) for point in selection["points"]]
        draw.line(vertices, fill=ink, width=3)
        for x, y in vertices:
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=ink)
    elif kind == "area":
        edge = selection["region"]
        around = (_scaled_xy((edge["left"], edge["top"]), size, origin),
                  _scaled_xy((edge["right"], edge["bottom"]), size, origin))
        draw.rectangle(around, outline=ink, width=3)


def _measurement_caption(content: dict):
    measurement = content["measurement"]
    if measurement is None:
        return "Whole image"
    kind = content["selection"]["kind"]
    unit = content.get("surface_unit", content["rail_unit"])
    if kind == "pin":
        value = measurement["position"]
        if measurement["reliable"]:
            return f"Point  ·  {value:.3f} {unit}  ·  confidence {measurement['confidence']:.2f}"
        return "Point  ·  insufficient focus evidence"
    if kind == "section":
        return f"Section  ·  {measurement['length_px']:.1f} px  ·  {measurement['reliable_fraction']:.1%} reliable"
    values = measurement["positions"]
    median = "—" if values["median"] is None else f"{values['median']:.3f} {unit}"
    return f"Area  ·  median {median}  ·  {measurement['reliable_fraction']:.1%} reliable"


def _wrapped_text(text: str, draw, face, max_width: int, limit: int = 3):
    lines = []
    buffer = ""
    for word in text.split():
        candidate = f"{buffer} {word}".strip()
        if draw.textlength(candidate, font=face) <= max_width:
            buffer = candidate
            continue
        if buffer:
            lines.append(buffer)
            buffer = word
        else:
            while len(word) > 1 and draw.textlength(word + "…", font=face) > max_width:
                word = word[:-1]
            buffer = word + "…"
        if len(lines) == limit:
            break
    if buffer and len(lines) < limit:
        lines.append(buffer)
    return lines


def visual_card(directory: Path, card: dict):
    content = card["content"]
    canvas = Image.new("RGB", CARD_PAGE, CARD_BG)
    draw = ImageDraw.Draw(canvas)
    face = ImageFont.load_default(size=19)
    draw.text((40, 31), card["title"][:66], fill=CARD_INK, font=ImageFont.load_default(size=30))
    caption = f"{content['layer'].capitalize()}   ·   {content['reference_name']}   ·   Revision {content['source_revision']}"
    draw.text((43, 77), caption, fill=CARD_INK, font=face)
    draw.rounded_rectangle((40, 124, 960, 624), fill=(26, 30, 42), radius=12)
    with Image.open(directory / RUN_ASSETS[content["layer"]]) as raw:
        picture = raw.convert("RGBA")
    picture.thumbnail((920, 500))
    origin = (40 + (920 - picture.width) // 2, 124 + (500 - picture.height) // 2)
    canvas.paste(picture, origin, picture)
    _draw_selection(draw, content["selection"], picture.size, origin)
    draw.text((43, 646), _measurement_caption(content), fill=CARD_INK, font=face)
    for index, row in enumerate(_wrapped_text(content["note"], draw, face, 895)):
        draw.text((43, 684 + index * 23), row, fill=CARD_INK, font=face)
    return canvas
