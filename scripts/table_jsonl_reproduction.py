"""Losslessly dehydrate or rehydrate JSONL table HTML using the source PDFs.

An exact table extraction can replace the entire HTML. Otherwise, exact cell
text occurrences are replaced by #literal_N# references in dehydrated_table_html;
unmatched content remains in that template. This is NOT a content-free format.
"""

import argparse
from collections import OrderedDict
from copy import deepcopy
from importlib import metadata
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import hydrate_tables as tables
import table_cell_reproduction as cells


DEFAULT_PDF_DIRS = tables.DEFAULT_PDF_DIRS
RECIPE = "table_literal_reproduction"
TEMPLATE = "dehydrated_table_html"
LEGACY_TEMPLATE = "hydrated_table_html"
# Recognize markup without mistaking comparisons such as '<5' for tags.
# Quoted attributes may themselves contain > characters.
TAG = re.compile(
    r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9:-]*)\b"
    r'''(?:"[^"]*"|'[^']*'|[^'"<>])*>''', re.IGNORECASE,
)
PLACEHOLDER = re.compile(r"#literal_([0-9]+)#")


def version(package):
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "unknown"


class Extractor:
    """Extract candidate table HTML with either Docling or PyMuPDF."""

    def __init__(self, paths, cache_size, engine):
        self.paths = paths
        self.engine = engine
        self.cache_size = cache_size
        self.docling = tables.TableExtractor(paths, cache_size) if engine == "docling" else None
        self.pdf_cache = cells.PdfCache(paths, cache_size)
        self.native_cache = OrderedDict()

    def get(self, bulletin_id, page):
        if self.docling is not None:
            return self.docling.get(bulletin_id)
        key = (bulletin_id, page)
        if key not in self.native_cache:
            import pymupdf as fitz
            pdf = self.pdf_cache.get(bulletin_id)
            candidates = []
            with fitz.open(self.paths[bulletin_id]) as document:
                if not 1 <= page <= len(document):
                    raise ValueError("table page is outside the PDF")
                for table in document[page - 1].find_tables().tables:
                    rows = table.extract()
                    value = "".join(
                        "<tr>" + "".join("<td>" + html.escape(cell or "", quote=False) + "</td>"
                                          for cell in row) + "</tr>" for row in rows
                    )
                    candidates.append({"page": page, "bbox": tuple(table.bbox), "table_html": value})
            self.native_cache[key] = (pdf.sha256, candidates)
            if len(self.native_cache) > self.cache_size:
                self.native_cache.popitem(last=False)
        self.native_cache.move_to_end(key)
        return self.native_cache[key]


def check_source(record, digest):
    for name in ("text_reproduction", "text_reconstruction", "table_rehydration"):
        item = record.get(name)
        if isinstance(item, dict) and item.get("pdf_sha256") not in (None, digest):
            raise ValueError(f"source PDF SHA-256 does not match {name}")


def extract_at_location(record, container, extractor):
    page, box = tables.locator(container)
    if page is None or box is None:
        raise ValueError("table page and bbox are required for whole-table extraction")
    digest, candidates = extractor.get(record["bulletin_id"], page)
    check_source(record, digest)
    candidate, _ = tables.select_table(candidates, page, box, None)
    return digest, candidate["table_html"]


def template_for(value, pdf):
    """Reference complete exact text segments, never approximate or stitch words."""
    references = {}
    by_value = {}
    next_id = 0

    def replace_text(segment):
        nonlocal next_id
        # Preserve formatting whitespace rather than matching across cell edges.
        stripped = segment.strip()
        if not stripped:
            return segment
        start = len(segment) - len(segment.lstrip())
        end = start + len(stripped)
        ref = pdf.find(stripped)
        if ref is None:
            decoded = html.unescape(stripped)
            if decoded != stripped and html.escape(decoded, quote=False) == stripped:
                ref = pdf.find(decoded)
                if ref is not None:
                    ref = {**ref, "escape": "html"}
        if ref is None:
            return segment
        if stripped not in by_value:
            while f"#literal_{next_id}#" in value:
                next_id += 1
            marker = f"#literal_{next_id}#"
            next_id += 1
            by_value[stripped] = marker
            references[marker] = ref
        return segment[:start] + by_value[stripped] + segment[end:]

    pieces = []
    in_cell = False
    cursor = 0
    for match in TAG.finditer(value):
        text = value[cursor:match.start()]
        pieces.append(replace_text(text) if in_cell else text)
        closing, name = match.group(1), match.group(2).lower()
        if name in {"td", "th"}:
            in_cell = not bool(closing)
        pieces.append(match.group())
        cursor = match.end()
    tail = value[cursor:]
    pieces.append(replace_text(tail) if in_cell else tail)
    return "".join(pieces), references


def restore_template(template, recipe, pdf):
    if not isinstance(template, str) or cells.digest(template) != recipe.get("template_sha256"):
        raise ValueError("table template SHA-256 does not match")
    references = recipe.get("literals")
    if not isinstance(references, dict):
        raise ValueError("invalid table literal references")
    replacements = {}
    for marker, ref in references.items():
        if not isinstance(marker, str) or not PLACEHOLDER.fullmatch(marker) or not isinstance(ref, dict):
            raise ValueError("invalid table placeholder or reference")
        if set(ref) not in ({"page_index", "start", "end"}, {"page_index", "start", "end", "escape"}):
            raise ValueError("invalid table character reference")
        page, start, end = (ref[key] for key in ("page_index", "start", "end"))
        if any(type(x) is not int for x in (page, start, end)) or not (
            0 <= page < len(pdf.pages) and 0 <= start < end <= len(pdf.pages[page])
        ):
            raise ValueError("table character reference outside PDF")
        text = pdf.pages[page][start:end]
        if "escape" in ref:
            if ref["escape"] != "html":
                raise ValueError("unsupported table literal escape")
            text = html.escape(text, quote=False)
        replacements[marker] = text
    # Replace only recorded markers, once. An original cell may itself contain
    # placeholder-looking text and a replacement may also contain a marker.
    for marker in replacements:
        if marker not in template:
            raise ValueError("table placeholder missing from template")
    restored = PLACEHOLDER.sub(lambda m: replacements.get(m.group(), m.group()), template)
    if cells.digest(restored) != recipe.get("html_sha256"):
        raise ValueError("reconstructed table HTML SHA-256 does not match")
    return restored


def dehydrate_record(record, extractor, cache, keep_authorized_table=False):
    row = deepcopy(record)
    container = tables.table_container(row)
    if container is None:
        return row, "skipped"
    if container is not row and "table_html" in row:
        raise ValueError("both top-level and nested table_html are present")
    if RECIPE in container or TEMPLATE in container or LEGACY_TEMPLATE in container:
        raise ValueError("table literal recipe/template already exists")
    if "table_reproduction" in container or "table_cell_reproduction" in container:
        raise ValueError("existing table recipe: rehydrate it before using this script")
    value = container.get("table_html")
    if not isinstance(value, str):
        raise ValueError("missing or invalid table_html")
    bulletin_id = row.get("bulletin_id")
    if not tables.valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    keep_html = keep_authorized_table and row.get("partial_reproduction") is True

    # Character references and PDF hash are needed even if extraction fails.
    pdf = cache.get(bulletin_id)
    check_source(row, pdf.sha256)
    extraction_error = None
    try:
        digest, produced = extract_at_location(row, container, extractor)
        if digest != pdf.sha256:
            raise ValueError("source PDF changed between extraction and character indexing")
        if produced == value:
            container[RECIPE] = {
                "version": 1, "method": "table_extraction", "engine": extractor.engine,
                "engine_version": version("docling" if extractor.engine == "docling" else "pymupdf"),
                "pdf_sha256": pdf.sha256, "html_sha256": cells.digest(value),
            }
            if not keep_html:
                del container["table_html"]
            return row, "kept_authorized" if keep_html else "exact"
    except Exception as exc:
        extraction_error = str(exc)
        print(f"{bulletin_id!r}: table extraction unavailable: {exc}", file=sys.stderr)

    template, references = template_for(value, pdf)
    recipe = {
        "version": 1, "method": "pdf_character_literals", "extraction": cells.EXTRACTION,
        "pdf_sha256": pdf.sha256, "html_sha256": cells.digest(value),
        "template_sha256": cells.digest(template), "pymupdf_version": pdf.version,
        "mupdf_version": pdf.mupdf_version, "source_scope": "entire_pdf", "literals": references,
    }
    if restore_template(template, recipe, pdf) != value:
        raise ValueError("table template failed its round-trip check")
    container[TEMPLATE] = template
    container[RECIPE] = recipe
    if not keep_html:
        del container["table_html"]
    if keep_html:
        return row, "kept_authorized"
    return row, "template" if extraction_error is None else "template_extraction_failed"


def rehydrate_record(record, extractor, cache):
    row = deepcopy(record)
    container = tables.table_container(row)
    if container is None or RECIPE not in container:
        return row, "skipped"
    recipe = container[RECIPE]
    if not isinstance(recipe, dict) or recipe.get("version") != 1:
        raise ValueError("invalid table literal recipe")
    bulletin_id = row.get("bulletin_id")
    if not tables.valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    pdf = cache.get(bulletin_id)
    check_source(row, pdf.sha256)
    if recipe.get("pdf_sha256") != pdf.sha256:
        raise ValueError("source PDF SHA-256 does not match table recipe")
    method = recipe.get("method")
    if method == "table_extraction":
        if extractor.engine != recipe.get("engine"):
            raise ValueError(f"table recipe requires --engine {recipe.get('engine')}")
        digest, value = extract_at_location(row, container, extractor)
        if digest != pdf.sha256 or cells.digest(value) != recipe.get("html_sha256"):
            raise ValueError("re-extracted table HTML or PDF SHA-256 does not match")
    elif method == "pdf_character_literals":
        if recipe.get("extraction") != cells.EXTRACTION:
            raise ValueError("unsupported PDF character extraction")
        if TEMPLATE in container and LEGACY_TEMPLATE in container:
            raise ValueError("conflicting table templates")
        template = container.get(TEMPLATE, container.get(LEGACY_TEMPLATE))
        value = restore_template(template, recipe, pdf)
    else:
        raise ValueError("unsupported table literal recipe method")
    if "table_html" in container and container["table_html"] != value:
        raise ValueError("existing table_html differs from reconstructed HTML")
    container["table_html"] = value
    return row, "restored"


def process(mode, source, output, directories, engine="docling", cache_size=4, limit=None, overwrite=False,
            keep_authorized_table=False):
    if source.resolve() == output.resolve():
        raise ValueError("input and output must be different files")
    if output.exists() and not overwrite:
        raise ValueError(f"output already exists: {output}; use --overwrite")
    if not any(path.is_dir() for path in directories):
        raise ValueError("no PDF directories exist; specify --pdf-dir")
    from itertools import islice

    def selected():
        return islice(tables.records_in(source), limit)

    ids = {row["bulletin_id"] for _, row in selected() if tables.valid_id(row.get("bulletin_id"))}
    pdfs = tables.find_pdfs(directories, ids)
    cache = cells.PdfCache(pdfs, size=cache_size)
    extractor = Extractor(pdfs, cache_size, engine)
    counts = {key: 0 for key in ("exact", "template", "template_extraction_failed", "kept_authorized",
                                  "restored", "skipped", "failed")}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=f".{output.name}.", suffix=".tmp", delete=False) as dst:
            temp = Path(dst.name)
            for number, original in selected():
                try:
                    row, status = (dehydrate_record(original, extractor, cache, keep_authorized_table) if mode == "dehydrate"
                                   else rehydrate_record(original, extractor, cache))
                    counts[status] += 1
                    if status == "template_extraction_failed":
                        print(f"Line {number}: table extraction unavailable; used cell references", file=sys.stderr)
                except Exception as exc:
                    row = original
                    counts["failed"] += 1
                    print(f"Line {number} ({original.get('bulletin_id')!r}): {exc}", file=sys.stderr)
                dst.write(json.dumps(row, ensure_ascii=False) + "\n")
        if overwrite:
            os.replace(temp, output)
        else:
            os.link(temp, output)
            temp.unlink()
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dehydrate", "rehydrate"))
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pdf-dir", type=Path, action="append", help="PDF root; repeatable")
    parser.add_argument("--engine", choices=("docling", "pymupdf"), default="docling")
    parser.add_argument("--cache-size", type=int, default=4)
    parser.add_argument("--limit", type=int, help="Process only the first N records")
    parser.add_argument("--keep-authorized-table", action="store_true",
                        help="Keep table_html when partial_reproduction is true, while saving its recipe")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.cache_size < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--cache-size and --limit must be positive")
    if args.mode == "rehydrate" and args.keep_authorized_table:
        parser.error("--keep-authorized-table only applies to dehydration")
    try:
        counts = process(args.mode, args.input, args.output, args.pdf_dir or DEFAULT_PDF_DIRS,
                         args.engine, args.cache_size, args.limit, args.overwrite, args.keep_authorized_table)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}; " + "; ".join(f"{key} {value}" for key, value in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
