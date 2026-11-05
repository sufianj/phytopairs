"""Restore legacy table cell recipes using source-PDF character references.

For literal-free dehydration of all tables, use dehydrate_tables.py instead.
"""

import argparse
from bisect import bisect_left, bisect_right
from collections import OrderedDict
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

from hydrate_tables import DEFAULT_PDF_DIRS, find_pdfs, locator, records_in, table_container, valid_id
from text_reconstruction import EXTRACTION, file_sha256, page_data


TAG = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)\b([^<>]*?)>")
ATTRIBUTE = re.compile(r'''\s+(?:rowspan|colspan)\s*=\s*(?:"[0-9]+"|'[0-9]+'|[0-9]+)''', re.I)
ENTITY = re.compile(r"&(?:\#x[0-9a-fA-F]+|\#X[0-9a-fA-F]+|\#[0-9]+|[A-Za-z][A-Za-z0-9]+);")
TOKENS = re.compile(r"[^\W_]+|[\W_]+", re.UNICODE)
TAGS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "b", "i", "em", "strong", "u", "sup", "sub", "br", "span"}


def unaccent(value):
    return "".join(char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char))


TRANSFORMS = {
    "lower": str.lower,
    "upper": str.upper,
    "title": str.title,
    "unaccent": unaccent,
    "unaccent_lower": lambda value: unaccent(value).lower(),
    "unaccent_upper": lambda value: unaccent(value).upper(),
    "unaccent_title": lambda value: unaccent(value).title(),
}
CASE_TRANSFORMS = {"same": lambda value: value, "lower": str.lower, "upper": str.upper, "title": str.title}


def accent_case(source, spec):
    """Apply a source-preserving case/diacritic edit without storing letters."""
    if not isinstance(spec, dict) or set(spec) != {"case", "marks", "form"}:
        raise ValueError("invalid accent/case transformation")
    case = spec["case"]
    operation = CASE_TRANSFORMS.get(case) if isinstance(case, str) else None
    if (operation is None or not isinstance(spec["form"], str) or spec["form"] not in {"NFC", "NFD"}
            or not isinstance(spec["marks"], list)):
        raise ValueError("invalid accent/case transformation")
    result = list(operation(unaccent(source)))
    previous = -1
    for item in spec["marks"]:
        if not isinstance(item, dict) or set(item) != {"position", "marks"}:
            raise ValueError("invalid accent positions")
        position, marks = item["position"], item["marks"]
        if (isinstance(position, bool) or not isinstance(position, int) or not previous < position < len(result)
                or not isinstance(marks, str) or not marks or not all(unicodedata.combining(c) for c in marks)):
            raise ValueError("invalid accent positions")
        result[position] += marks
        previous = position
    return unicodedata.normalize(spec["form"], "".join(result))


def accent_case_spec(source, target):
    decomposed = unicodedata.normalize("NFKD", target)
    base, marks = [], []
    for char in decomposed:
        if unicodedata.combining(char):
            if not base:
                return None
            if marks and marks[-1]["position"] == len(base) - 1:
                marks[-1]["marks"] += char
            else:
                marks.append({"position": len(base) - 1, "marks": char})
        else:
            base.append(char)
    for name, operation in CASE_TRANSFORMS.items():
        if operation(unaccent(source)) != "".join(base):
            continue
        for form in ("NFC", "NFD"):
            spec = {"case": name, "marks": marks, "form": form}
            if accent_case(source, spec) == target:
                return spec
    return None


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def html_segments(value):
    """Split a table fragment into exact markup and cell-text segments.

    This recognizes actual tags rather than treating unescaped comparisons
    like '< 5' or '<1' as tags. Unknown or unsafe markup is rejected.
    """
    result, stack = [], []
    cursor = 0
    cell_number = 0
    current_cell = None
    for match in TAG.finditer(value):
        before = value[cursor:match.start()]
        if before:
            if current_cell is None:
                if before.strip():
                    raise ValueError("text or unsupported markup outside a table cell")
                result.append(("markup", before, None))
            else:
                result.append(("cell", before, current_cell))
        closing, name, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if name not in TAGS:
            raise ValueError(f"unsupported HTML tag {name!r}")
        self_closing = attrs.rstrip().endswith("/")
        attrs = attrs.rstrip()[:-1] if self_closing else attrs
        if closing and (attrs.strip() or self_closing):
            raise ValueError("invalid closing tag")
        if not closing and name in {"td", "th"}:
            remainder = ATTRIBUTE.sub("", attrs)
            if remainder.strip():
                raise ValueError("unsupported cell attributes")
            if current_cell is not None:
                raise ValueError("nested table cells are unsupported")
            current_cell = cell_number
            cell_number += 1
        elif not closing and attrs.strip():
            raise ValueError("unsupported HTML attributes")
        if name == "br":
            if closing:
                raise ValueError("invalid closing br")
        elif closing:
            if not stack or stack[-1] != name:
                raise ValueError("unbalanced HTML tags")
            stack.pop()
            if name in {"td", "th"}:
                current_cell = None
        elif not self_closing:
            stack.append(name)
        else:
            raise ValueError("unsupported self-closing tag")
        result.append(("markup", match.group(), None))
        cursor = match.end()
    tail = value[cursor:]
    truncated_tag = False
    if tail:
        if current_cell is None:
            if tail.strip():
                # Some reviewed tables were cut off mid-tag. Preserve the
                # unfinished tag prefix exactly, but never treat arbitrary
                # trailing prose as content-free markup.
                prefix = re.fullmatch(r"<\s*/?\s*([A-Za-z]*)", tail.strip())
                if not prefix or not any(tag.startswith(prefix.group(1).lower()) for tag in TAGS):
                    raise ValueError("text or unsupported markup outside a table cell")
                truncated_tag = True
            result.append(("markup", tail, None))
        else:
            result.append(("cell", tail, current_cell))
    if (current_cell is not None or not cell_number
            or (stack and not (truncated_tag and all(tag in {"table", "thead", "tbody", "tfoot", "tr"} for tag in stack)))):
        raise ValueError("incomplete HTML table or no cells")
    return result, cell_number


class PdfCharacters:
    def __init__(self, path):
        import pymupdf as fitz
        self.sha256 = file_sha256(path)
        self.pages, self.boundaries = [], []
        with fitz.open(path) as pdf:
            if pdf.is_encrypted:
                raise ValueError("PDF is encrypted")
            for page in pdf:
                text, _, _, boundaries = page_data(page, page.get_textpage())
                self.pages.append(text)
                self.boundaries.append(boundaries)
        self.version = fitz.VersionBind
        self.mupdf_version = fitz.VersionFitz
        self.matches = {}
        self.transformed_index = None

    def find(self, value):
        """Find the first exact, whole-word occurrence in the PDF text layer."""
        if value in self.matches:
            return self.matches[value]
        found = None
        for page_index, text in enumerate(self.pages):
            start = text.find(value)
            while start != -1:
                end = start + len(value)
                bounds = self.boundaries[page_index]
                left_ok = not value[0].isalnum() or start == 0 or not text[start - 1].isalnum() or start in bounds
                right_ok = not value[-1].isalnum() or end == len(text) or not text[end].isalnum() or end in bounds
                if left_ok and right_ok:
                    found = {"page_index": page_index, "start": start, "end": end}
                    break
                start = text.find(value, start + 1)
            if found is not None:
                break
        self.matches[value] = found
        return found

    def find_transformed(self, value):
        """Find an entire source word differing only by case or accents."""
        if self.transformed_index is None:
            self.transformed_index = {}
            for page, text in enumerate(self.pages):
                bounds = sorted(self.boundaries[page])
                for match in re.finditer(r"[^\W_]+", text):
                    start, end = match.span()
                    # Formatting span boundaries can separate adjacent words
                    # in the raw character stream without an actual space.
                    cuts = [start] + bounds[bisect_right(bounds, start):bisect_left(bounds, end)] + [end]
                    locations = {(start, end)} | set(zip(cuts, cuts[1:]))
                    for left, right in sorted(locations):
                        word = text[left:right]
                        key = unaccent(word).casefold()
                        self.transformed_index.setdefault(key, []).append((page, left, right, word))
        for page, start, end, source in self.transformed_index.get(unaccent(value).casefold(), []):
            for name, operation in TRANSFORMS.items():
                if operation(source) == value:
                    return {"page_index": page, "start": start, "end": end, "transform": name}
            spec = accent_case_spec(source, value)
            if spec is not None:
                return {"page_index": page, "start": start, "end": end, "accent_case": spec}
        return None


class PdfCache:
    def __init__(self, paths, size=4, docling_extractor=None):
        self.paths, self.size = paths, size
        self.cache = OrderedDict()
        self.docling_cache = OrderedDict()
        self.docling_extractor = docling_extractor

    def get(self, bulletin_id):
        if bulletin_id not in self.cache:
            path = self.paths.get(bulletin_id)
            if path is None:
                raise ValueError(f"PDF not found for bulletin_id {bulletin_id!r}")
            try:
                self.cache[bulletin_id] = PdfCharacters(path)
            except Exception as exc:
                self.cache[bulletin_id] = ValueError(f"Cannot read PDF {path}: {exc}")
            if len(self.cache) > self.size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(bulletin_id)
        value = self.cache[bulletin_id]
        if isinstance(value, Exception):
            raise value
        return value

    def get_docling_source(self, bulletin_id):
        if bulletin_id not in self.docling_cache:
            path = self.paths.get(bulletin_id)
            if path is None:
                raise ValueError(f"PDF not found for bulletin_id {bulletin_id!r}")
            try:
                from hydrate_tables import TableExtractor
                from table_ocr_source import DoclingTextSource
                if self.docling_extractor is None:
                    self.docling_extractor = TableExtractor(self.paths, self.size)
                self.docling_cache[bulletin_id] = DoclingTextSource(path, self.docling_extractor.get_converter())
            except Exception as exc:
                self.docling_cache[bulletin_id] = ValueError(f"Cannot extract Docling text from {path}: {exc}")
            if len(self.docling_cache) > self.size:
                self.docling_cache.popitem(last=False)
        self.docling_cache.move_to_end(bulletin_id)
        value = self.docling_cache[bulletin_id]
        if isinstance(value, Exception):
            raise value
        return value


def add_literal(parts, value):
    if not value:
        return
    if parts and "literal" in parts[-1]:
        parts[-1]["literal"] += value
    else:
        parts.append({"literal": value})


def cell_recipe(text, pdf, allow_content_literals):
    parts, literals = [], 0
    # A full exact occurrence is preferred over separately matched words.
    exact = pdf.find(text) if any(c.isalnum() for c in text) and not ENTITY.search(text) else None
    if exact:
        return [exact], 0

    def words(piece):
        nonlocal literals
        for match in TOKENS.finditer(piece):
            token = match.group()
            if any(c.isalnum() for c in token):
                reference = pdf.find(token)
                if reference is None:
                    reference = pdf.find_transformed(token)
                if reference is not None:
                    parts.append(reference)
                else:
                    count = sum(c.isalnum() for c in token)
                    literals += count
                    add_literal(parts, token)
            else:
                add_literal(parts, token)

    offset = 0
    for match in ENTITY.finditer(text):
        words(text[offset:match.start()])
        entity = match.group()
        decoded = html.unescape(entity)
        if decoded == entity or any(c.isalnum() for c in decoded):
            raise ValueError("unsupported or alphanumeric HTML entity in a cell")
        # This is HTML syntax for punctuation, not a retained word or number.
        add_literal(parts, entity)
        offset = match.end()
    words(text[offset:])
    if literals and not allow_content_literals:
        raise ValueError(f"cell text needs {literals} literal letters or digits; table retained")
    return parts, literals


def restore_html(recipe, pages, docling_streams=None):
    if not isinstance(recipe, dict) or recipe.get("version") != 1 or recipe.get("extraction") != EXTRACTION:
        raise ValueError("invalid or unsupported table cell recipe")
    if not isinstance(recipe.get("parts"), list):
        raise ValueError("invalid table recipe parts")
    html_parts = []
    for part in recipe["parts"]:
        if not isinstance(part, dict):
            raise ValueError("invalid table recipe part")
        if set(part) == {"markup"} and isinstance(part["markup"], str):
            html_parts.append(part["markup"])
        elif set(part) == {"cell", "text"} and isinstance(part["cell"], int) and not isinstance(part["cell"], bool) and part["cell"] >= 0 and isinstance(part["text"], list):
            for piece in part["text"]:
                if not isinstance(piece, dict):
                    raise ValueError("invalid cell text part")
                if set(piece) == {"literal"} and isinstance(piece["literal"], str):
                    html_parts.append(piece["literal"])
                elif set(piece) in ({"page_index", "start", "end"}, {"page_index", "start", "end", "transform"},
                                    {"page_index", "start", "end", "accent_case"}, {"page_index", "start", "end", "escape"},
                                    {"docling_stream", "start", "end"}, {"docling_stream", "start", "end", "transform"},
                                    {"docling_stream", "start", "end", "escape"}):
                    ocr = "docling_stream" in piece
                    streams = docling_streams if ocr else pages
                    if streams is None:
                        raise ValueError("Docling text streams required by the table recipe")
                    page, start, end = (piece[key] for key in (("docling_stream" if ocr else "page_index"), "start", "end"))
                    if any(isinstance(x, bool) or not isinstance(x, int) for x in (page, start, end)):
                        raise ValueError("invalid PDF character offsets")
                    if not 0 <= page < len(streams) or not 0 <= start < end <= len(streams[page]):
                        raise ValueError("source character reference out of bounds")
                    text = streams[page][start:end]
                    if "transform" in piece:
                        transform = piece["transform"]
                        operation = TRANSFORMS.get(transform) if isinstance(transform, str) else None
                        if operation is None:
                            raise ValueError("unsupported cell text transform")
                        text = operation(text)
                    if "accent_case" in piece:
                        text = accent_case(text, piece["accent_case"])
                    if "escape" in piece:
                        if piece["escape"] != "html":
                            raise ValueError("unsupported HTML entity encoding")
                        text = html.escape(text, quote=True)
                    html_parts.append(text)
                else:
                    raise ValueError("invalid cell text part")
        else:
            raise ValueError("invalid table recipe part")
    result = "".join(html_parts)
    if digest(result) != recipe.get("html_sha256"):
        raise ValueError("reconstructed table HTML SHA-256 does not match")
    return result


def dehydrate_record(record, cache, allow_content_literals=False, keep_partial=False):
    container = table_container(record)
    if container is None:
        return "skipped"
    page, box = locator(container)
    if page is not None and box is not None:
        return "skipped"
    if keep_partial and record.get("partial_reproduction") is True:
        return "skipped"
    value = container.get("table_html")
    if value is None and isinstance(container.get("table_cell_reproduction"), dict):
        return "skipped"
    if not isinstance(value, str) or not value:
        raise ValueError("missing table_html")
    if "table_cell_reproduction" in container:
        raise ValueError("existing table_cell_reproduction would be overwritten")
    bulletin_id = record.get("bulletin_id")
    if not valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    segments, cells = html_segments(value)
    pdf = cache.get(bulletin_id)
    text_recipe = record.get("text_reproduction", record.get("text_reconstruction"))
    if isinstance(text_recipe, dict) and text_recipe.get("pdf_sha256") and text_recipe["pdf_sha256"] != pdf.sha256:
        raise ValueError("source PDF SHA-256 does not match the text recipe")
    parts, literal_count = [], 0
    for kind, segment, cell in segments:
        if kind == "markup":
            parts.append({"markup": segment})
        else:
            source, count = cell_recipe(segment, pdf, allow_content_literals)
            parts.append({"cell": cell, "text": source})
            literal_count += count
    recipe = {"version": 1, "extraction": EXTRACTION, "pdf_sha256": pdf.sha256,
              "html_sha256": digest(value), "source_scope": "entire_pdf",
              "pymupdf_version": pdf.version, "mupdf_version": pdf.mupdf_version,
              "cell_count": cells, "literal_alnum_count": literal_count,
              "source_html_truncated": not value.rstrip().endswith(">"), "parts": parts}
    if restore_html(recipe, pdf.pages) != value:
        raise ValueError("table cell recipe failed its round-trip check")
    container["table_cell_reproduction"] = recipe
    del container["table_html"]
    return "dehydrated"


def rehydrate_record(record, cache):
    container = table_container(record)
    if container is None:
        return "skipped"
    recipe = container.get("table_cell_reproduction")
    if recipe is None and isinstance(container.get("table_html"), str):
        return "skipped"
    if not isinstance(recipe, dict):
        raise ValueError("missing table_cell_reproduction recipe")
    bulletin_id = record.get("bulletin_id")
    if not valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    pdf = cache.get(bulletin_id)
    if recipe.get("pdf_sha256") != pdf.sha256:
        raise ValueError("source PDF SHA-256 does not match the table recipe")
    docling_streams = None
    if "docling_text_source" in recipe:
        source = cache.get_docling_source(bulletin_id)
        if source.sha256 != pdf.sha256:
            raise ValueError("source PDF changed between PDF and Docling extraction")
        if recipe["docling_text_source"] != source.provenance():
            raise ValueError("Docling version or text-stream SHA-256 does not match the table recipe")
        docling_streams = source.pages
    value = restore_html(recipe, pdf.pages, docling_streams)
    existing = container.get("table_html")
    if existing is not None and existing != value:
        raise ValueError("existing table_html differs from the reconstructed table")
    container["table_html"] = value
    return "rehydrated"


def process(mode, source, output, directories, overwrite=False, allow_content_literals=False, keep_partial=False):
    if source.resolve() == output.resolve():
        raise ValueError("Input and output must be different files")
    if output.exists() and not overwrite:
        raise ValueError(f"Output already exists: {output}; use --overwrite")
    if not any(path.is_dir() for path in directories):
        raise ValueError("No PDF directories exist; specify --pdf-dir")
    ids = set()
    for _, row in records_in(source):
        container = table_container(row)
        if container is None or not valid_id(row.get("bulletin_id")):
            continue
        if mode == "rehydrate" and isinstance(container.get("table_cell_reproduction"), dict):
            ids.add(row["bulletin_id"])
        elif mode == "dehydrate" and (container.get("page") is None or container.get("bbox") is None):
            ids.add(row["bulletin_id"])
    cache = PdfCache(find_pdfs(directories, ids))
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = {"dehydrated": 0, "rehydrated": 0, "skipped": 0, "failed": 0}
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=f".{output.name}.", suffix=".tmp", delete=False) as dst:
            temp = Path(dst.name)
            for number, row in records_in(source):
                try:
                    status = (dehydrate_record(row, cache, allow_content_literals, keep_partial)
                              if mode == "dehydrate" else rehydrate_record(row, cache))
                    counts[status] += 1
                    if status == "dehydrated":
                        literals = table_container(row)["table_cell_reproduction"]["literal_alnum_count"]
                        if literals:
                            print(f"Line {number}: warning: recipe retains {literals} literal letters or digits", file=sys.stderr)
                except (ValueError, OSError) as exc:
                    counts["failed"] += 1
                    print(f"Line {number} ({row.get('bulletin_id')!r}): {exc}", file=sys.stderr)
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
    parser.add_argument("--pdf-dir", action="append", type=Path, help="PDF root to search recursively; repeatable")
    parser.add_argument("--allow-content-literals", action="store_true", help="Retain unmatched letters and digits in the recipe (not content-free)")
    parser.add_argument("--keep-partial-reproduction", action="store_true", help="Leave tables marked partial_reproduction=true unchanged")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.mode == "dehydrate":
        print("Error: legacy cell dehydration may retain table content; use scripts/dehydrate_tables.py instead", file=sys.stderr)
        return 2
    if args.mode == "rehydrate" and (args.allow_content_literals or args.keep_partial_reproduction):
        parser.error("dehydration options cannot be used with rehydrate")
    try:
        counts = process(args.mode, args.input, args.output, args.pdf_dir or DEFAULT_PDF_DIRS,
                         args.overwrite, args.allow_content_literals, args.keep_partial_reproduction)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}; dehydrated {counts['dehydrated']}; rehydrated {counts['rehydrated']}; skipped {counts['skipped']}; failed {counts['failed']}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
