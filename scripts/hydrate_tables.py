"""Restore JSONL tables from cell recipes or re-extract them with Docling.

Pages are 1-based; boxes are [left, top, right, bottom] in PDF points.
Re-extraction is not guaranteed to reproduce the original HTML byte for byte.
"""

import argparse
from collections import OrderedDict
from difflib import SequenceMatcher
import hashlib
import html
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata


DEFAULT_PDF_DIRS = (Path(__file__).resolve().parents[1] / "data" / "local" / "pdfs",)


def records_in(path):
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {number}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected an object on line {number}")
            yield number, record


def valid_id(value):
    return isinstance(value, str) and bool(value) and value not in {".", ".."} and Path(value).name == value


def find_pdfs(directories, ids):
    found = {}
    for directory in directories:
        if not directory.is_dir():
            print(f"Warning: PDF directory does not exist: {directory}", file=sys.stderr)
            continue
        remaining = ids - found.keys()
        for bulletin_id in remaining:
            path = directory / (bulletin_id + ".pdf")
            if path.is_file():
                found[bulletin_id] = path
        remaining = ids - found.keys()
        if remaining:
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() == ".pdf" and path.stem in remaining and path.is_file():
                    found[path.stem] = path
                    remaining.remove(path.stem)
                    if not remaining:
                        break
    return found


def table_container(record):
    """Return the owner of the locator and HTML, or None for non-table records."""
    table = record.get("table")
    if isinstance(table, dict):
        return table
    if table is not None:
        raise ValueError("table must be an object")
    if record.get("pair_type") == "text_table" or "table_html" in record:
        return record
    return None


def locator(container):
    page = container.get("page")
    if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
        raise ValueError("page must be a positive 1-based integer")
    box = container.get("bbox")
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError("bbox must be [left, top, right, bottom]")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in box):
            raise ValueError("bbox coordinates must be finite numbers")
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("bbox has non-positive area")
        box = tuple(box)
    return page, box


def table_to_html(table):
    """Use the same cell-grid and HTML serialization as bsv_parser.py."""
    cells = table.data.table_cells
    if not cells:
        return None
    rows = max(c.end_row_offset_idx for c in cells)
    cols = max(c.end_col_offset_idx for c in cells)
    if rows <= 0 or cols <= 0:
        return None
    grid = [[None] * cols for _ in range(rows)]
    for cell in cells:
        grid[cell.start_row_offset_idx][cell.start_col_offset_idx] = cell
    result, covered = [], set()
    for r in range(rows):
        result.append("<tr>")
        for c in range(cols):
            if (r, c) in covered:
                continue
            cell = grid[r][c]
            if cell is None:
                result.append("<td></td>")
                continue
            for rr in range(cell.start_row_offset_idx, cell.end_row_offset_idx):
                for cc in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                    covered.add((rr, cc))
            attrs = ""
            if cell.row_span > 1:
                attrs += f' rowspan="{cell.row_span}"'
            if cell.col_span > 1:
                attrs += f' colspan="{cell.col_span}"'
            tag = "th" if cell.column_header else "td"
            result.append(f"<{tag}{attrs}>{cell.text}</{tag}>")
        result.append("</tr>")
    return "".join(result)


def page_box(bbox, width, height, source_width, source_height):
    """Convert Docling provenance to top-left PDF point coordinates."""
    origin = getattr(bbox.coord_origin, "value", bbox.coord_origin)
    if origin == "BOTTOMLEFT":
        top, bottom = source_height - bbox.t, source_height - bbox.b
    elif origin == "TOPLEFT":
        top, bottom = bbox.t, bbox.b
    else:
        raise ValueError(f"unknown Docling coordinate origin {origin!r}")
    box = (bbox.l * width / source_width, top * height / source_height,
           bbox.r * width / source_width, bottom * height / source_height)
    if not all(math.isfinite(x) for x in box) or box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("invalid Docling table bounding box")
    return box


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class TableExtractor:
    def __init__(self, paths, cache_size=4, native_only=False):
        self.paths = paths
        self.cache_size = cache_size
        self.native_only = native_only
        self.cache = OrderedDict()
        self.converter = None

    def get_converter(self):
        if self.converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions, EasyOcrOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
            options = PdfPipelineOptions()
            options.do_ocr = True
            options.ocr_options = EasyOcrOptions(lang=["fr"])
            options.do_table_structure = True
            self.converter = DocumentConverter(format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            })
        return self.converter

    def get(self, bulletin_id):
        if bulletin_id not in self.cache:
            path = self.paths.get(bulletin_id)
            if path is None:
                raise ValueError(f"PDF not found for bulletin_id {bulletin_id!r}")
            try:
                import pymupdf as fitz
                with fitz.open(path) as pdf:
                    if pdf.is_encrypted:
                        raise ValueError("PDF is encrypted")
                    sizes = [(p.rect.width, p.rect.height) for p in pdf]
                candidates = []
                if self.native_only:
                    with fitz.open(path) as pdf:
                        for page_number, pdf_page in enumerate(pdf, 1):
                            for table in pdf_page.find_tables().tables:
                                text = " ".join(str(cell or "") for row in table.extract() for cell in row)
                                if text.strip():
                                    # Escaping keeps literal '<' from being mistaken for an HTML tag.
                                    candidates.append({"page": page_number, "bbox": tuple(table.bbox),
                                                       "table_html": html.escape(text)})
                else:
                    # The construction parser converted the entire PDF, not just a page.
                    conversion = self.get_converter().convert(path)
                    status = getattr(getattr(conversion, "status", None), "value", getattr(conversion, "status", None))
                    if status is not None and str(status).lower() not in {"success", "partial_success"}:
                        raise ValueError(f"Docling conversion status: {status}")
                    if str(status).lower() == "partial_success":
                        raise ValueError("Docling conversion was incomplete")
                    document = conversion.document
                    for table in document.tables:
                        if not table.prov:
                            continue
                        prov = table.prov[0]  # Same provenance selection as the construction parser.
                        page = prov.page_no
                        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(sizes):
                            continue
                        table_html = table_to_html(table)
                        if not table_html:
                            continue
                        width, height = sizes[page - 1]
                        page_info = document.pages.get(page)
                        size = getattr(page_info, "size", None)
                        source_width = getattr(size, "width", None) or width
                        source_height = getattr(size, "height", None) or height
                        box = page_box(prov.bbox, width, height, source_width, source_height)
                        candidates.append({"page": page, "bbox": box, "table_html": table_html})
                value = (sha256(path), candidates)
            except Exception as exc:
                value = ValueError(f"Cannot extract tables from {path}: {exc}")
            self.cache[bulletin_id] = value
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(bulletin_id)
        value = self.cache[bulletin_id]
        if isinstance(value, Exception):
            raise value
        return value


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return intersection / (area_a + area_b - intersection)


def normalized_html(value):
    # Some original table cells contain unescaped comparisons such as "< 50".
    # Strip actual tags, not arbitrary text between '<' and the next '>'.
    text = html.unescape(re.sub(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s+[^<>]*?)?\s*/?>", " ", value))
    text = text.casefold().replace("œ", "oe").replace("æ", "ae")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if c.isalnum())


def select_table(candidates, page, box, existing_html, min_similarity=0.9):
    candidates = [c for c in candidates if page is None or c["page"] == page]
    query = normalized_html(existing_html) if existing_html else ""
    ranked = []
    for candidate in candidates:
        overlap = iou(box, candidate["bbox"]) if box is not None else None
        if overlap is not None and overlap < 0.5:
            continue
        similarity = None
        if query:
            target = normalized_html(candidate["table_html"])
            similarity = SequenceMatcher(None, query, target, autojunk=False).ratio()
            if similarity < min_similarity:
                continue
        score = overlap if overlap is not None else (similarity if similarity is not None else 1.0)
        ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        raise ValueError("no sufficiently matching table found")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.10:
        raise ValueError("ambiguous Docling tables; supply a page, bbox, or matching table HTML")
    return ranked[0][1], ranked[0][0]


def restore_record(record, extractor):
    container = table_container(record)
    if container is None:
        return "skipped"
    page, box = locator(container)
    existing_html = container.get("table_html")
    if existing_html is not None and not isinstance(existing_html, str):
        raise ValueError("table_html must be a string")
    table_recipe = container.get("table_reproduction")
    if table_recipe is not None:
        if (not isinstance(table_recipe, dict) or table_recipe.get("version") != 1
                or table_recipe.get("method") != "docling_bbox"
                or not isinstance(table_recipe.get("pdf_sha256"), str)
                or not isinstance(table_recipe.get("html_sha256"), str)):
            raise ValueError("invalid table_reproduction recipe")
        if page is None or box is None:
            raise ValueError("Docling table recipe requires both page and bbox")
        if existing_html is not None and hashlib.sha256(existing_html.encode("utf-8")).hexdigest() != table_recipe["html_sha256"]:
            raise ValueError("existing table HTML SHA-256 does not match the recipe")
    if existing_html and page is not None and box is not None:
        return "skipped"
    bulletin_id = record.get("bulletin_id")
    if not valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    native_only = getattr(extractor, "native_only", False)
    if native_only and table_recipe is not None:
        raise ValueError("Docling table recipes cannot be restored with --native-only")
    if native_only and not existing_html:
        raise ValueError("PyMuPDF location matching requires existing table_html")
    if "table_rehydration" in record and table_recipe is None:
        raise ValueError("existing table_rehydration metadata would be overwritten")
    if "table_rehydration" in record and not isinstance(record["table_rehydration"], dict):
        raise ValueError("invalid existing table_rehydration metadata")
    digest, candidates = extractor.get(bulletin_id)
    recipe = record.get("text_reproduction", record.get("text_reconstruction"))
    if isinstance(recipe, dict) and recipe.get("pdf_sha256") and recipe["pdf_sha256"] != digest:
        raise ValueError("source PDF SHA-256 does not match the text recipe")
    if table_recipe is not None and table_recipe["pdf_sha256"] != digest:
        raise ValueError("source PDF SHA-256 does not match the table recipe")
    previous_metadata = record.get("table_rehydration")
    if isinstance(previous_metadata, dict) and previous_metadata.get("pdf_sha256") not in (None, digest):
        raise ValueError("source PDF SHA-256 does not match existing table metadata")
    candidate, score = select_table(candidates, page, box, existing_html,
                                    min_similarity=0.95 if native_only else 0.9)
    if table_recipe is not None:
        actual_hash = hashlib.sha256(candidate["table_html"].encode("utf-8")).hexdigest()
        if actual_hash != table_recipe["html_sha256"]:
            raise ValueError("re-extracted table HTML SHA-256 does not match the table recipe")
    if not existing_html:
        container["table_html"] = candidate["table_html"]
    if page is None:
        container["page"] = candidate["page"]
    if box is None:
        container["bbox"] = list(candidate["bbox"])
    try:
        version = metadata.version("pymupdf" if native_only else "docling")
    except metadata.PackageNotFoundError:
        version = "unknown"
    metadata_record = {
        "method": "pymupdf_find_tables" if native_only else "docling",
        "pdf_sha256": digest,
        "match_basis": "bbox_iou" if box is not None else ("table_html" if existing_html else "unique_candidate"),
        "match_score": round(score, 6),
        "detected_page": candidate["page"], "detected_bbox": list(candidate["bbox"]),
    }
    if native_only:
        metadata_record["pymupdf_version"] = version
    else:
        metadata_record.update({"docling_version": version, "ocr_engine": "easyocr",
                                "ocr_languages": ["fr"]})
    if "table_rehydration" not in record:
        record["table_rehydration"] = metadata_record
    return "restored"


def process(input_path, output_path, directories, overwrite=False, cache_size=4, native_only=False,
            cells_only=False):
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Input and output must be different files")
    if output_path.exists() and not overwrite:
        raise ValueError(f"Output already exists: {output_path}; use --overwrite to replace it")
    if not any(directory.is_dir() for directory in directories):
        raise ValueError("No PDF directories exist; specify the correct paths with --pdf-dir")
    ids = set()
    for _, record in records_in(input_path):
        if not valid_id(record.get("bulletin_id")):
            continue
        if cells_only:
            try:
                container = table_container(record)
            except ValueError:
                continue  # The record is reported during processing.
            if container is None or "table_cell_reproduction" not in container:
                continue
        ids.add(record["bulletin_id"])
    pdfs = find_pdfs(directories, ids)
    extractor = TableExtractor(pdfs, cache_size, native_only)
    cell_cache = None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"restored": 0, "cells_restored": 0, "skipped": 0, "failed": 0}
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent,
                                         prefix=f".{output_path.name}.", suffix=".tmp", delete=False) as output:
            temp_path = Path(output.name)
            for number, record in records_in(input_path):
                try:
                    container = table_container(record)
                    if container is not None and "table_cell_reproduction" in container:
                        if "table_reproduction" in container:
                            raise ValueError("conflicting table reproduction recipes")
                        if not isinstance(container["table_cell_reproduction"], dict):
                            raise ValueError("invalid table_cell_reproduction recipe")
                        # Cell recipes take precedence; never guess another table
                        # or attempt location recovery after reconstructing one.
                        if cell_cache is None:
                            from table_cell_reproduction import PdfCache, rehydrate_record
                            cell_cache = PdfCache(pdfs, size=cache_size)
                        rehydrate_record(record, cell_cache)
                        status = "cells_restored"
                    elif cells_only:
                        status = "skipped"
                    else:
                        status = restore_record(record, extractor)
                    counts[status] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    print(f"Line {number} ({record.get('bulletin_id')!r}): {exc}", file=sys.stderr)
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        if overwrite:
            os.replace(temp_path, output_path)
        else:
            os.link(temp_path, output_path)
            temp_path.unlink()
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input JSONL dataset")
    parser.add_argument("output", type=Path, nargs="?", help="Output JSONL (default: INPUT_STEM_tables_hydrated.jsonl)")
    parser.add_argument("--pdf-dir", action="append", type=Path, help="PDF root to search recursively; repeatable")
    parser.add_argument("--cache-size", type=int, default=4, help="Number of converted PDFs to cache (default: 4)")
    parser.add_argument("--native-only", action="store_true",
                        help="Find locations using PyMuPDF table detection and existing HTML, without Docling")
    parser.add_argument("--cells-only", action="store_true",
                        help="Only restore cell recipes; skip Docling/native table location recovery")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()
    if args.cache_size < 1:
        parser.error("--cache-size must be positive")
    output = args.output or args.input.with_name(f"{args.input.stem}_tables_hydrated.jsonl")
    try:
        counts = process(args.input, output, args.pdf_dir or DEFAULT_PDF_DIRS, args.overwrite, args.cache_size,
                         args.native_only, args.cells_only)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {output}; restored tables/locations {counts['restored']}; "
          f"restored cell recipes {counts['cells_restored']}; skipped {counts['skipped']}; failed {counts['failed']}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
