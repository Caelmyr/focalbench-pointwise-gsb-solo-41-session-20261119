# FocalBench

FocalBench is a local inspection workbench for macro and microscope focus stacks of manufactured objects. It keeps the original captures, aligns the focal planes, builds one all-in-focus image and records where the images contain enough focus evidence for a surface estimate.

## Run locally

With Docker and Docker Compose:

```sh
docker compose up --build -d
```

Open [the workbench](http://127.0.0.1:18944/) or check [service health](http://127.0.0.1:18944/api/health). The named `focalbench-data` volume keeps the stack library and archived results. `docker compose stop` stops the service and preserves the library. `FOCALBENCH_PORT=19944 docker compose -p focalbench-check up -d` uses another host port and a separately named volume.

With a local Python 3.11 or 3.12 environment, install `requirements.txt` and run `./start.sh`. Set `FOCALBENCH_DATA_DIR` for a different library path or `PORT` for a different local port. The default data directory outside Compose is `runtime-data/`.

## First inspection

1. Open **Soldered controller** or **Machined shim and gauge** from the library. Both are deterministic synthetic targets. The first contains unevenly spaced captures. The second has noticeable change of image scale along the rail.
2. Page through the **Capture queue**, use **Preflight** to check frame dimensions, support, rail positions and exposure, then open **Reconstruction** on the capture panel to pick the reference and recipe.
3. Select **Reconstruct** and switch between **All-in-focus**, **Focus surface**, **Confidence**, **Reliable coverage**, **Winning plane** and **Frame overlap**. In the result, select **Point** and click a detail to see the focus response across captures.
4. Select **Area** and drag a rectangle for a depth histogram and coverage readings. Select **Section** and click two endpoints for a depth profile. A section can be downloaded as CSV.
5. Open **Planes** to display the reliable contributions from an individual capture. Open **Compare** to see a registered capture pair or compare two archived recipes with the same reference capture.
6. Use **Notebook** to annotate a point, section or area and save an inspection reading. The **Evidence ZIP** includes maps, metadata, capture evidence, annotations and requested section data.

## Capture and planning tools

- Create a stack and batch-import PNG, JPEG, single-page TIFF or WebP images with positions. Edit capture notes and inclusion or retake an existing slot. A retake preserves the prior source for archived results.
- Preview and apply measured rail logs using a UTF-8 `filename,position` header. Optional `unit` accepts `µm`, `mm` or `step`. Duplicate filenames, unmatched rows and colliding enabled positions are reported before applying.
- Generate a focus sweep from an observed sharp interval, requested overlap, physical span and frame/time budget. Compare the plan with the current rail log and download the ordered positions.
- Inspect a source histogram and clipping map, or export a labeled contact sheet.
- Export an active capture stack as a portable ZIP and re-import it on a different installation. Rebuilding uses the same original source bytes, current positions and recipe. Fork an active stack or a frozen result to review changes in a new stack.

## Inspection measurements

- Image alignment estimates bounded translation and optional isotropic breathing relative to a single capture. The shared-coverage crop is the largest uninterrupted rectangle covered by every included frame. The reference view shows where only part of the stack contributes.
- Focus response comes from local detail and derivative energy in linear luminance. The surface uses rail position at the strongest response and can fit intermediate values between nearby, supported samples. Low-confidence areas, holes and rail-boundary peaks need inspection.
- The rail origin is a display offset set manually or estimated from a selected datum patch. An XY scale is measured from a known image span or supplied as original-reference µm per pixel. Saved results preserve original settings, crop offsets and reference geometry.
- When the rail only reports stage steps, an optional **measured step-and-height calibration** records two to twelve step/height pairs. Capture positions stay in steps; every archived surface reading, point reading, section profile, chart, plane list and 16-bit depth PNG then uses the same micrometre scale, with heights linearly interpolated between measured pairs. A focus peak that lands between two captures is fitted continuously inside that interval. Captures outside the first and last pair still compose the all-in-focus image, but their pixels carry no surface depth (depth PNG code 0, no point reading). Each archived result keeps the calibration it was generated with, so reopening it does not follow a later edit.
- A selected patch can be exported at native result resolution with RGB and confidence maps, a float depth TIFF and, for up to 32,000 pixels, a coordinate CSV. The standard unsigned 16-bit depth PNG uses code 0 for no data. Codes 1 through 65535 linearly encode the near-to-far values described in the archived manifest.
- Capture pair and recipe comparisons operate in the recorded reference coordinates. The surface is a focus-derived estimate affected by lighting, texture, occlusion and optics. Metric interpretation depends on the capture and calibration.

## Input and resource limits

A stack retains up to 64 current or archived captures, with 3–32 included in a reconstruction. One file can contain up to 22 MiB and 14 million oriented pixels, and each side must be at least 48 pixels. Upload at most 16 images at once. Enabled captures need matching oriented dimensions and distinct rail positions. RGB is processed in linear sRGB, grayscale accepts unsigned samples up to 16 bits, and near-opaque pixels provide support for processing. Camera RAW, multi-page acquisitions in one file, rotation and perspective correction are outside these inputs.

Reconstruction uses at most 1,400 pixels on the long side and a 30 million-pixel total across included frames. It may reduce the working raster before processing. Results retain several maps and volume-wide response samples for repeatable inspection, so larger stacks consume more disk per result. One reconstruction runs at a time. A portable bundle supports up to 96 MiB of unique source bytes.

Browser results, PNGs and ZIPs are generated by the local application. The examples describe synthetic captures and their expected behavior.

## Source map

`focalbench/web.py` starts the server. `stacks_api.py` handles capture management and acquisition, while `results_api.py` exposes the archived outputs and measurements. The processing pipeline is in `reconstruction.py` with image decoding, rail alignment, focus selection and fusion in `imaging.py`, `alignment.py`, `focus.py` and `fusion.py`. `store.py` maintains SQLite revisions and original image bytes. The independent archive inspection, planning and report code consumes the saved coordinate frame and validity maps.

Recompute effective backend lines with:

```sh
python3 scripts/count_backend.py --project-root . --source-root focalbench
```
