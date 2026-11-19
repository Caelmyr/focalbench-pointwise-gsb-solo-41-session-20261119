"""Archived product assets and image measurements used by the bench."""

from flask import Blueprint, request, send_file

from .attribution import contribution_image, contribution_summary, create_attribution, run_image_integrity
from .calibration import commit_estimate, parse_spans, project_estimate
from .compare import compare_metrics, pair_from_run, render_pair
from .coverage import coverage_report, show_deficits
from .datum import apply_datum, inspect_datum
from .diagnostics import characterize_focus
from .http import (
    engine, file_response, file_title, library, optional_payload, payload, png_response,
    region_of, region_query, revision,
)
from .models import (
    Mark, Region, Unprocessable, choice, flag, integer, number, parse_points_query, parse_vertex,
    require_id, short_text, validate_keys,
)
from .notebook import create_card, visual_card
from .patches import make_patch, possible_patch_outputs
from .reports import annotations_csv, capture_report, evidence_package, profile_report, report_sheet
from .surface import Inspection, measure_mark
from .transfer import fork_result
from .variants import comparison_report, compare_results, describe_comparison, result_comparison_image

results = Blueprint("results", __name__, url_prefix="/api/stacks/<project_id>/results")


def archived(project_id, run_id):
    run = library().run(project_id, run_id)
    return run, Inspection(library().run_directory(run_id), run["summary"])


@results.post("")
def reconstruct(project_id):
    data = optional_payload()
    validate_keys(data, {"name", "revision"})
    title = short_text(data.get("name", "Focus reconstruction"), "result name", maximum=110)
    return engine().build(project_id, title, revision(data)), 201


@results.get("/<run_id>")
def result_details(project_id, run_id):
    return library().run(project_id, run_id)


@results.post("/<run_id>/fork")
def result_to_stack(project_id, run_id):
    data = payload()
    validate_keys(data, {"name"})
    title = short_text(data.get("name"), "stack name")
    run = library().run(project_id, run_id)
    return fork_result(library(), run, title), 201


@results.get("/<run_id>/images/<layer>")
def result_image(project_id, run_id, layer):
    path = library().run_asset(project_id, run_id, layer)
    attachment = request.args.get("download") == "1"
    mode = "image/webp" if layer == "overview" else "image/png"
    return send_file(path, mimetype=mode, as_attachment=attachment, max_age=31_536_000)


@results.get("/<run_id>/captures.csv")
def result_capture_report(project_id, run_id):
    run = library().run(project_id, run_id)
    filename = file_title(run["title"], "-capture-evidence.csv")
    return file_response(capture_report(run["summary"]), "text/csv", filename)


@results.post("/<run_id>/point")
def inspect_point(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    data = payload()
    validate_keys(data, {"point"})
    return inspection.sample(parse_vertex(data.get("point"), "point"))


@results.post("/<run_id>/area")
def inspect_area(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    data = payload()
    validate_keys(data, {"region", "bins"})
    region = Region.parse(data.get("region"))
    bars = integer(data.get("bins", 18), "histogram bins", 5, 60)
    return inspection.area(region, bars)


@results.post("/<run_id>/section")
def inspect_section(project_id, run_id):
    run, inspection = archived(project_id, run_id)
    data = payload()
    validate_keys(data, {"points", "samples", "download"})
    points = parse_points_query(data.get("points"))
    size = integer(data.get("samples", 80), "samples", 16, 350)
    section = inspection.cross_section(points, size)
    if flag(data.get("download", False), "download"):
        title = file_title(run["title"], "-section.csv")
        return file_response(profile_report(section), "text/csv", title)
    return section


@results.post("/<run_id>/sharp-interval")
def estimate_sharpness_width(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    data = payload()
    validate_keys(data, {"region", "threshold"})
    region = Region.parse(data.get("region"))
    threshold = number(data.get("threshold", 0.65), "contrast fraction", 0.3, 0.95)
    return characterize_focus(inspection, region, threshold)


@results.post("/<run_id>/patch")
def result_patch(project_id, run_id):
    run, inspection = archived(project_id, run_id)
    data = payload()
    validate_keys(data, {"region", "csv", "download"})
    region = Region.parse(data.get("region"))
    include_csv = flag(data.get("csv", False), "sample CSV")
    if flag(data.get("download", False), "download"):
        content = make_patch(inspection, run, region, include_csv)
        return file_response(content, "application/zip", file_title(run["title"], "-patch.zip"))
    return possible_patch_outputs(inspection, region)


@results.post("/<run_id>/calibration")
def calibrate_plane(project_id, run_id):
    run = library().run(project_id, run_id)
    data = payload()
    validate_keys(data, {"spans", "apply", "revision"})
    spans = parse_spans(data.get("spans"))
    estimate = project_estimate(run, spans)
    if flag(data.get("apply", False), "apply"):
        changed = commit_estimate(library(), project_id, estimate, revision(data))
        return {"measurement": estimate, "stack": changed}
    return estimate


@results.post("/<run_id>/deficits")
def analyze_coverage(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    data = optional_payload()
    validate_keys(data, {"region", "kind", "minimum"})
    kind = choice(data.get("kind", "both"), "type of deficit", ("both", "missing", "weak"))
    minimum = integer(data.get("minimum", 80), "minimum region pixels", 1, 1_000_000)
    return coverage_report(inspection, kind, minimum, region_of(data))


@results.get("/<run_id>/deficits.png")
def display_deficits(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    kind = choice(request.args.get("kind", "both"), "type of deficit", ("both", "missing", "weak"))
    minimum = integer(request.args.get("minimum", 80), "minimum region pixels", 1, 1_000_000)
    return png_response(show_deficits(inspection, kind, minimum, region_query()))


@results.get("/<run_id>/planes/<capture_id>")
def plane_contribution(project_id, run_id, capture_id):
    run, inspection = archived(project_id, run_id)
    source = create_attribution(inspection, run, capture_id)
    minimum = integer(request.args.get("minimum", 24), "minimum island pixels", 1, 1_000_000)
    return contribution_summary(inspection, source, minimum)


@results.get("/<run_id>/planes/<capture_id>/image")
def plane_markup(project_id, run_id, capture_id):
    run, inspection = archived(project_id, run_id)
    source = create_attribution(inspection, run, capture_id)
    view = choice(request.args.get("view", "highlight"), "plane view", ("highlight", "registered", "mask"))
    heading = file_title(source.capture["name"], "-plane.png") if request.args.get("download") else None
    return png_response(contribution_image(library(), run, source, view), heading)


def _requested_pair(run, data, registered):
    first = require_id(data.get("first"), "first capture")
    second = require_id(data.get("second"), "second capture")
    return pair_from_run(library(), run, first, second, registered=registered)


@results.post("/<run_id>/pair")
def evaluate_pair(project_id, run_id):
    run = library().run(project_id, run_id)
    data = payload()
    validate_keys(data, {"first", "second", "registered", "region"})
    mode = flag(data.get("registered", True), "registered")
    pair = _requested_pair(run, data, mode)
    return compare_metrics(pair, region_of(data))


@results.get("/<run_id>/pair.png")
def preview_pair(project_id, run_id):
    run = library().run(project_id, run_id)
    args = request.args
    alignment = choice(args.get("alignment", "registered"), "pair alignment", ("registered", "raw"))
    pair = _requested_pair(run, args, alignment == "registered")
    view = choice(args.get("mode", "overlay"), "pair display", ("split", "overlay", "difference", "side"))
    amount = number(args.get("amount", 0.5), "display amount", 0.0, 1.0)
    return png_response(render_pair(pair, view, amount))


@results.get("/<run_id>/source-integrity")
def source_checks(project_id, run_id):
    run = library().run(project_id, run_id)
    return run_image_integrity(library(), run)


@results.get("/<run_id>/marks")
def annotations(project_id, run_id):
    return {"marks": library().list_marks(project_id, run_id)}


@results.post("/<run_id>/marks")
def create_annotation(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    mark = Mark.parse(payload())
    calculated = measure_mark(inspection, mark)
    return library().add_mark(project_id, run_id, mark, calculated), 201


@results.delete("/<run_id>/marks/<mark_id>")
def delete_annotation(project_id, run_id, mark_id):
    library().remove_mark(project_id, run_id, mark_id)
    return "", 204


@results.get("/<run_id>/cards")
def inspection_cards(project_id, run_id):
    return {"cards": library().review_cards(project_id, run_id)}


@results.post("/<run_id>/cards")
def add_reading(project_id, run_id):
    run = library().run(project_id, run_id)
    return create_card(library(), project_id, run, payload()), 201


@results.delete("/<run_id>/cards/<card_id>")
def delete_reading(project_id, run_id, card_id):
    library().delete_card(project_id, run_id, card_id)
    return "", 204


@results.get("/<run_id>/cards/<card_id>/proof.png")
def card_proof(project_id, run_id, card_id):
    card = library().review_card(project_id, run_id, card_id)
    image = visual_card(library().run_directory(run_id), card)
    title = file_title(card["title"], "-inspection-card.png") if request.args.get("download") else None
    return png_response(image, title)


@results.get("/<run_id>/proof.png")
def inspection_proof(project_id, run_id):
    run = library().run(project_id, run_id)
    marks = library().list_marks(project_id, run_id)
    image = report_sheet(library(), run, marks)
    title = file_title(run["title"], "-inspection-proof.png") if request.args.get("download") else None
    return png_response(image, title)


@results.get("/<run_id>/evidence")
def export_evidence(project_id, run_id):
    run = library().run(project_id, run_id)
    title = file_title(run["title"], "-evidence.zip")
    return file_response(evidence_package(library(), project_id, run_id), "application/zip", title)


@results.post("/<run_id>/variants")
def compare_archived_recipes(project_id, run_id):
    data = payload()
    validate_keys(data, {"other", "download", "steps"})
    primary, secondary = result_variant_inputs(project_id, run_id, data.get("other"))
    comparison = compare_results(primary, secondary)
    if flag(data.get("download", False), "download"):
        steps = integer(data.get("steps", 36), "grid steps", 8, 120)
        return file_response(comparison_report(comparison, steps), "text/csv", "focalbench-recipe-comparison.csv")
    return describe_comparison(comparison)


@results.get("/<run_id>/variants.png")
def show_archived_recipes(project_id, run_id):
    view = choice(request.args.get("view", "split"), "comparison view", ("split", "support", "difference"))
    amount = number(request.args.get("amount", 0.5), "blend control", 0, 1)
    primary, secondary = result_variant_inputs(project_id, run_id, request.args.get("other"))
    comparison = compare_results(primary, secondary)
    return png_response(result_comparison_image(comparison, view, amount))


def result_variant_inputs(project_id, run_id, other):
    alternate = require_id(other, "comparison result")
    if alternate == run_id:
        raise Unprocessable("Compare this result with a different reconstruction.")
    _, primary = archived(project_id, run_id)
    _, secondary = archived(project_id, alternate)
    return primary, secondary


@results.post("/<run_id>/datum")
def result_datum(project_id, run_id):
    _, inspection = archived(project_id, run_id)
    data = payload()
    validate_keys(data, {"region", "position", "apply", "revision"})
    region = Region.parse(data.get("region"))
    requested = number(data["position"], "datum reading", -1e7, 1e7) if "position" in data else None
    measurement = inspect_datum(inspection, region, requested)
    if flag(data.get("apply", False), "apply"):
        stack = apply_datum(library(), project_id, inspection, measurement, revision(data))
        return {"measurement": measurement, "stack": stack}
    return measurement

@results.get("/<run_id>/marks.csv")
def annotations_download(project_id, run_id):
    run = library().run(project_id, run_id)
    name = file_title(run["title"], "-annotations.csv")
    marks = library().list_marks(project_id, run_id)
    return file_response(
        annotations_csv(marks, run["summary"].get("surface_unit", run["summary"]["scale"]["rail_unit"])),
        "text/csv", name,
    )
