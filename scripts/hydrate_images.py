"""Restore image crops referenced by a JSONL dataset from its source PDFs."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile

import pymupdf as fitz
from PIL import Image as PILImage


DEFAULT_PDF_DIRS = (
    Path("/projects/agrifutur/bsv_pdfs"),
    Path("/projects/agrifutur/bsv_pdfs_all"),
)


def records_in(path):
    """Yield (line number, source line, parsed record), ignoring blank lines."""
    with path.open(encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected a JSON object on line {line_number}")
            yield line_number, raw_line, record


def find_pdfs(directories, bulletin_ids):
    """Index matching PDFs, preferring directories in the given order."""
    result = {}
    for directory in directories:
        if not directory.is_dir():
            print(f"Warning: PDF directory does not exist: {directory}", file=sys.stderr)
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".pdf" and path.stem in bulletin_ids:
                result.setdefault(path.stem, path)
    return result


def coordinate(image, name):
    """Read a coordinate, allowing accidental whitespace in JSON keys."""
    if name in image:
        value = image[name]
    else:
        value = next((v for key, v in image.items() if str(key).strip() == name), None)
    if value is None:
        raise ValueError(f"Missing image coordinate {name!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Image coordinate {name!r} must be finite")
    return number


def crop_image(document, image, destination, dpi, jpeg_quality):
    page_number = image.get("page")
    if isinstance(page_number, bool):
        raise ValueError("Image page must be a 1-based page number")
    try:
        page_number = int(page_number)
    except (TypeError, ValueError) as exc:
        raise ValueError("Image page must be a 1-based page number") from exc
    if page_number < 1 or page_number > document.page_count:
        raise ValueError(f"Page {page_number} is outside the PDF (1-{document.page_count})")

    left = coordinate(image, "left")
    top = coordinate(image, "top")
    right = coordinate(image, "right")
    bottom = coordinate(image, "bottom")
    if right <= left or bottom <= top:
        raise ValueError(f"Invalid bounding box: left={left}, top={top}, right={right}, bottom={bottom}")

    page = document[page_number - 1]
    clip = fitz.Rect(left, top, right, bottom) & page.rect
    if clip.is_empty or clip.width <= 0 or clip.height <= 0:
        raise ValueError(f"Bounding box is outside page {page_number}")

    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    suffix = destination.suffix.lower()
    if suffix == ".png":
        image_bytes = pixmap.tobytes("png")
    elif suffix in {".jpg", ".jpeg"}:
        image_bytes = pixmap.tobytes("jpeg", jpg_quality=jpeg_quality)
    elif suffix == ".jpx":
        # PyMuPDF cannot encode JPEG 2000 directly; Pillow can when built with
        # OpenJPEG support. Keep the JSONL filename and use a matching format.
        from io import BytesIO

        rgb_image = PILImage.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        encoded = BytesIO()
        rgb_image.save(encoded, format="JPEG2000")
        image_bytes = encoded.getvalue()
    else:
        raise ValueError(f"Unsupported image extension {suffix!r}; use .jpg, .jpeg, .jpx, or .png")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image_bytes)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Restore image crops for JSONL records using each record's bulletin_id, "
            "image.page, and image bounding box (PDF point coordinates)."
        )
    )
    parser.add_argument("input", type=Path, help="Input JSONL dataset")
    parser.add_argument(
        "--output",
        type=Path,
        help="Hydrated JSONL output (default: INPUT_STEM_hydrated.jsonl)",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        help="Directory for restored images (default: OUTPUT_STEM_images beside output)",
    )
    parser.add_argument(
        "--pdf-dir",
        action="append",
        type=Path,
        help=(
            "PDF root to search recursively; may be repeated. Defaults to "
            "/projects/agrifutur/bsv_pdfs and /projects/agrifutur/bsv_pdfs_all."
        ),
    )
    parser.add_argument("--dpi", type=int, default=200, help="Rendered crop resolution (default: 200 DPI)")
    parser.add_argument(
        "--jpeg-quality", type=int, default=95, help="JPEG quality from 1 to 100 (default: 95)"
    )
    args = parser.parse_args()

    if args.dpi <= 0:
        parser.error("--dpi must be positive")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("--jpeg-quality must be between 1 and 100")
    if not args.input.is_file():
        parser.error(f"Input JSONL does not exist: {args.input}")

    output = args.output or args.input.with_name(f"{args.input.stem}_hydrated.jsonl")
    image_dir = args.image_dir or output.with_name(f"{output.stem}_images")
    pdf_dirs = args.pdf_dir if args.pdf_dir is not None else list(DEFAULT_PDF_DIRS)

    # Scan once to build an index only for bulletin IDs that occur in this dataset.
    bulletin_ids = set()
    for line_number, _, record in records_in(args.input):
        bulletin_id = record.get("bulletin_id")
        if bulletin_id:
            bulletin_ids.add(str(bulletin_id))
    pdf_by_id = find_pdfs(pdf_dirs, bulletin_ids)

    output.parent.mkdir(parents=True, exist_ok=True)
    document_by_id = {}
    failures = 0
    restored = 0
    skipped = 0
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False
        ) as destination_file:
            temp_path = Path(destination_file.name)
            for line_number, raw_line, record in records_in(args.input):
                bulletin_id = str(record.get("bulletin_id") or "")
                image = record.get("image")
                if not isinstance(image, dict):
                    skipped += 1
                    destination_file.write(raw_line if raw_line.endswith("\n") else raw_line + "\n")
                    continue

                pdf_path = pdf_by_id.get(bulletin_id)
                try:
                    if pdf_path is None:
                        raise FileNotFoundError(f"No PDF found for bulletin_id {bulletin_id!r}")
                    document = document_by_id.get(bulletin_id)
                    if document is None:
                        document = fitz.open(pdf_path)
                        document_by_id[bulletin_id] = document

                    filename = image.get("filename")
                    if filename:
                        image_name = Path(str(filename)).name
                    else:
                        page = image.get("page", "unknown")
                        image_name = f"{bulletin_id}_p{page}_{line_number}.jpeg"
                    crop_image(document, image, image_dir / image_name, args.dpi, args.jpeg_quality)
                    restored += 1
                except Exception as exc:
                    failures += 1
                    print(f"Line {line_number} ({bulletin_id}): {exc}", file=sys.stderr)

                # The record schema and locator metadata are preserved as provided.
                destination_file.write(raw_line if raw_line.endswith("\n") else raw_line + "\n")

        os.replace(temp_path, output)
        temp_path = None
    finally:
        for document in document_by_id.values():
            document.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print(f"Hydrated JSONL: {output}")
    print(f"Restored images: {restored} in {image_dir}")
    print(f"Records without an image object: {skipped}")
    print(f"Failed image restores: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
