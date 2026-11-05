"""Remove table HTML only when exact, literal-free reconstruction is verified.

Use Docling with the stored location when it reproduces the HTML exactly;
otherwise try PDF character references, with optional Docling OCR references.
The output is not published if any table cannot be safely dehydrated.
"""

import argparse
from copy import deepcopy
from importlib import metadata
from itertools import islice
import json
import os
from pathlib import Path
import sys
import tempfile

import hydrate_tables as hydrator
import table_cell_reproduction as cells
from text_reconstruction import EXTRACTION


class IncompleteDehydration(ValueError):
    """One or more tables cannot be dehydrated without losing content."""


class CombinedSource:
    """Prefer PDF characters, then use verified Docling text streams."""

    def __init__(self, pdf, docling):
        self.pdf, self.docling = pdf, docling
        self.pages = pdf.pages
        self.sha256 = pdf.sha256
        self.version, self.mupdf_version = pdf.version, pdf.mupdf_version
        self.used_docling = False

    def find(self, value):
        reference = self.pdf.find(value)
        if reference is None:
            reference = self.docling.find(value)
            self.used_docling |= reference is not None
        return reference

    def find_transformed(self, value):
        reference = self.pdf.find_transformed(value)
        if reference is not None and "accent_case" in reference:
            reference = None
        if reference is None:
            reference = self.docling.find_transformed(value)
            if reference is not None and "accent_case" in reference:
                reference = None
            self.used_docling |= reference is not None
        return reference


def selected_records(path, limit, start_index=0):
    source = hydrator.records_in(path)
    yield from islice(source, start_index, None if limit is None else start_index + limit)


def literal_free_cell(text, pdf):
    """Copy each text character from the PDF; never embed cell literals."""
    parts = []

    def reference(value, allow_transform=False):
        found = pdf.find(value)
        if found is None and allow_transform:
            found = pdf.find_transformed(value)
            # Accent additions include literal combining marks; exclude them.
            if found is not None and "accent_case" in found:
                found = None
        return found

    def plain(piece):
        for match in cells.TOKENS.finditer(piece):
            token = match.group()
            found = reference(token, allow_transform=any(c.isalnum() for c in token))
            if found is not None:
                parts.append(found)
            elif any(c.isalnum() for c in token):
                raise ValueError("a cell word or number has no literal-free source reference")
            else:
                for character in token:
                    found = reference(character)
                    if found is None:
                        raise ValueError("cell punctuation or whitespace has no source reference")
                    parts.append(found)

    offset = 0
    for match in cells.ENTITY.finditer(text):
        plain(text[offset:match.start()])
        entity = match.group()
        decoded = cells.html.unescape(entity)
        if decoded == entity or cells.html.escape(decoded, quote=True) != entity:
            raise ValueError("HTML entity cannot be reproduced without a content literal")
        found = reference(decoded)
        if found is None:
            raise ValueError("HTML entity has no source character reference")
        parts.append({**found, "escape": "html"})
        offset = match.end()
    plain(text[offset:])
    return parts


def make_cell_recipe(value, pdf):
    segments, count = cells.html_segments(value)
    parts = []
    for kind, text, cell in segments:
        if kind == "markup":
            parts.append({"markup": text})
        else:
            parts.append({"cell": cell, "text": literal_free_cell(text, pdf)})
    recipe = {
        "version": 1, "extraction": EXTRACTION, "pdf_sha256": pdf.sha256,
        "html_sha256": cells.digest(value), "source_scope": "entire_pdf",
        "pymupdf_version": pdf.version, "mupdf_version": pdf.mupdf_version,
        "cell_count": count, "literal_alnum_count": 0, "literal_count": 0,
        "source_html_truncated": not value.rstrip().endswith(">"), "parts": parts,
    }
    streams = None
    if isinstance(pdf, CombinedSource) and pdf.used_docling:
        recipe["docling_text_source"] = pdf.docling.provenance()
        streams = pdf.docling.pages
    if cells.restore_html(recipe, pdf.pages, streams) != value:
        raise ValueError("literal-free cell recipe failed its round-trip check")
    return recipe


def docling_recipe(record, container, value, extractor):
    page, box = hydrator.locator(container)
    if page is None or box is None:
        raise ValueError("Docling reconstruction needs the table page and bbox")
    digest, candidates = extractor.get(record["bulletin_id"])
    original_text = record.get("text_reproduction", record.get("text_reconstruction"))
    if isinstance(original_text, dict) and original_text.get("pdf_sha256") not in (None, digest):
        raise ValueError("source PDF digest does not match the text recipe")
    if "table_rehydration" in record:
        previous = record["table_rehydration"]
        if not isinstance(previous, dict):
            raise ValueError("invalid existing table_rehydration metadata")
        if previous.get("pdf_sha256") not in (None, digest):
            raise ValueError("source PDF digest does not match existing table metadata")
    candidate, _ = hydrator.select_table(candidates, page, box, None)
    if candidate["table_html"] != value:
        raise ValueError("Docling did not reproduce the original table HTML exactly")
    try:
        version = metadata.version("docling")
    except metadata.PackageNotFoundError:
        version = "unknown"
    return {"version": 1, "method": "docling_bbox", "pdf_sha256": digest,
            "html_sha256": cells.digest(value), "docling_version": version,
            "ocr_engine": "easyocr", "ocr_languages": ["fr"]}


def dehydrate_record(record, extractor, pdf_cache, method="auto", ocr_fallback=False):
    """Return a new record; the input is untouched on failure."""
    row = deepcopy(record)
    container = hydrator.table_container(row)
    if container is None:
        return row, "skipped"
    if container is not row and "table_html" in row:
        raise ValueError("both top-level and nested table_html are present")
    bulletin_id = row.get("bulletin_id")
    if not hydrator.valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    if "table_cell_reproduction" in container and "table_reproduction" in container:
        raise ValueError("conflicting table reproduction recipes")

    # Existing legacy recipes may contain literals. Reconstruct the original
    # HTML first, then build and verify a new literal-free recipe.
    if "table_cell_reproduction" in container:
        cells.rehydrate_record(row, pdf_cache)
        del container["table_cell_reproduction"]
    elif "table_reproduction" in container:
        hydrator.restore_record(row, extractor)
        del container["table_reproduction"]

    value = container.get("table_html")
    if not isinstance(value, str) or not value:
        raise ValueError("missing table_html and no restorable recipe")

    docling_error = None
    if method in {"auto", "docling"}:
        try:
            container["table_reproduction"] = docling_recipe(row, container, value, extractor)
            del container["table_html"]
            return row, "docling"
        except Exception as exc:
            docling_error = str(exc)
            if method == "docling":
                raise ValueError(docling_error) from exc

    try:
        pdf = pdf_cache.get(bulletin_id)
        text_recipe = row.get("text_reproduction", row.get("text_reconstruction"))
        if isinstance(text_recipe, dict) and text_recipe.get("pdf_sha256") not in (None, pdf.sha256):
            raise ValueError("source PDF digest does not match the text recipe")
        try:
            recipe = make_cell_recipe(value, pdf)
        except ValueError:
            if not ocr_fallback:
                raise
            docling = pdf_cache.get_docling_source(bulletin_id)
            if docling.sha256 != pdf.sha256:
                raise ValueError("source PDF changed between PDF and Docling extraction")
            recipe = make_cell_recipe(value, CombinedSource(pdf, docling))
        container["table_cell_reproduction"] = recipe
        del container["table_html"]
        return row, "cells"
    except Exception as exc:
        if docling_error:
            raise ValueError(f"Docling: {docling_error}; cell references: {exc}") from exc
        raise ValueError(str(exc)) from exc


def process(input_path, output_path, directories, method="auto", limit=None, overwrite=False, cache_size=4,
            ocr_fallback=False, start_index=0):
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output must be different files")
    if output_path.exists() and not overwrite:
        raise ValueError(f"output already exists: {output_path}; use --overwrite")
    if not any(directory.is_dir() for directory in directories):
        raise ValueError("no PDF directories exist; specify --pdf-dir")
    ids = {row["bulletin_id"] for _, row in selected_records(input_path, limit, start_index)
           if hydrator.valid_id(row.get("bulletin_id"))}
    pdfs = hydrator.find_pdfs(directories, ids)
    extractor = hydrator.TableExtractor(pdfs, cache_size)
    cache = cells.PdfCache(pdfs, cache_size, docling_extractor=extractor)
    counts = {"docling": 0, "cells": 0, "skipped": 0, "failed": 0}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent,
                                         prefix=f".{output_path.name}.", suffix=".tmp", delete=False) as dst:
            temp = Path(dst.name)
            for number, record in selected_records(input_path, limit, start_index):
                if number == 1 or number % 10 == 0:
                    print(f"Processing line {number}...", file=sys.stderr, flush=True)
                try:
                    row, status = dehydrate_record(record, extractor, cache, method, ocr_fallback)
                    counts[status] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    print(f"Line {number} ({record.get('bulletin_id')!r}): {exc}", file=sys.stderr)
                    continue
                dst.write(json.dumps(row, ensure_ascii=False) + "\n")
        if counts["failed"]:
            raise IncompleteDehydration(f"{counts['failed']} table(s) failed; no output was published")
        if not sum(counts.values()):
            raise ValueError("no input records were selected")
        if overwrite:
            os.replace(temp, output_path)
        else:
            os.link(temp, output_path)
            temp.unlink()
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pdf-dir", action="append", type=Path, help="PDF root to search recursively; repeatable")
    parser.add_argument("--method", choices=("auto", "docling", "cells"), default="auto")
    parser.add_argument("--ocr-fallback", action="store_true",
                        help="Try Docling/EasyOCR text references when the PDF text layer is insufficient")
    parser.add_argument("--limit", type=int, help="Test only the first N records; output contains only those records")
    parser.add_argument("--start-index", type=int, default=0, help="Skip this many JSONL records before processing")
    parser.add_argument("--cache-size", type=int, default=4)
    parser.add_argument("--require-cuda", action="store_true",
                        help="Require CUDA-enabled PyTorch and configure Docling to use CUDA")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.cache_size < 1:
        parser.error("--cache-size must be positive")
    if args.start_index < 0:
        parser.error("--start-index must be non-negative")
    if args.require_cuda:
        if args.method == "cells" and not args.ocr_fallback:
            parser.error("--method cells does not use Docling or CUDA")
        try:
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA is unavailable in this PyTorch environment")
        except (ImportError, RuntimeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        os.environ["DOCLING_DEVICE"] = "cuda"
    try:
        counts = process(args.input, args.output, args.pdf_dir or hydrator.DEFAULT_PDF_DIRS,
                         args.method, args.limit, args.overwrite, args.cache_size, args.ocr_fallback, args.start_index)
    except IncompleteDehydration as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}; Docling recipes {counts['docling']}; cell recipes {counts['cells']}; skipped {counts['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
