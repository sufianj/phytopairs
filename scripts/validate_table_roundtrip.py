"""Verify table HTML is absent after dehydration and exactly restored afterward."""

import argparse
from itertools import zip_longest
from pathlib import Path
import re
import sys

import dehydrate_tables as dehydrate
import hydrate_tables as hydrator
import table_cell_reproduction as cells


def verify_cell_recipe(recipe):
    allowed = {"version", "extraction", "pdf_sha256", "html_sha256", "source_scope", "pymupdf_version",
               "mupdf_version", "cell_count", "literal_alnum_count", "literal_count", "source_html_truncated", "parts"}
    if (not isinstance(recipe, dict) or set(recipe) not in (allowed, allowed | {"docling_text_source"})
            or recipe.get("version") != 1
            or recipe.get("extraction") != cells.EXTRACTION or recipe.get("source_scope") != "entire_pdf"
            or type(recipe.get("literal_count")) is not int or recipe["literal_count"] != 0
            or type(recipe.get("literal_alnum_count")) is not int or recipe["literal_alnum_count"] != 0
            or type(recipe.get("cell_count")) is not int or recipe["cell_count"] < 1):
        raise ValueError("not a strict literal-free cell recipe")
    parts = recipe.get("parts")
    if not isinstance(parts, list):
        raise ValueError("invalid cell recipe parts")
    docling_source = recipe.get("docling_text_source")
    if docling_source is not None:
        source_keys = {"version", "extraction", "docling_version", "ocr_engine", "ocr_languages", "streams_sha256"}
        if (not isinstance(docling_source, dict) or set(docling_source) != source_keys
                or docling_source.get("version") != 1 or docling_source.get("extraction") != "docling-text-streams-v1"
                or not isinstance(docling_source.get("docling_version"), str)
                or docling_source.get("ocr_engine") != "easyocr" or docling_source.get("ocr_languages") != ["fr"]
                or not isinstance(docling_source.get("streams_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", docling_source["streams_sha256"])):
            raise ValueError("invalid Docling text-source metadata")
    markup = []
    used_docling = False
    for item in parts:
        if not isinstance(item, dict):
            raise ValueError("invalid cell recipe part")
        if set(item) == {"markup"} and isinstance(item["markup"], str):
            markup.append(item["markup"])
        elif (set(item) == {"cell", "text"} and isinstance(item["text"], list)
              and type(item["cell"]) is int and 0 <= item["cell"] < recipe["cell_count"]):
            for part in item["text"]:
                if not isinstance(part, dict) or set(part) not in (
                    {"page_index", "start", "end"}, {"page_index", "start", "end", "transform"},
                    {"page_index", "start", "end", "escape"},
                    {"docling_stream", "start", "end"}, {"docling_stream", "start", "end", "transform"},
                    {"docling_stream", "start", "end", "escape"},
                ):
                    raise ValueError("a cell recipe contains literals or unsupported data")
                used_docling |= "docling_stream" in part
                if "transform" in part and (not isinstance(part["transform"], str) or part["transform"] not in cells.TRANSFORMS):
                    raise ValueError("unsupported cell transform")
                if "escape" in part and part["escape"] != "html":
                    raise ValueError("unsupported entity transform")
        else:
            raise ValueError("invalid cell recipe part")
    segments, cell_count = cells.html_segments("".join(markup))
    if any(kind == "cell" for kind, _, _ in segments):
        raise ValueError("table structure contains cell text")
    if cell_count != recipe["cell_count"]:
        raise ValueError("table structure has an inconsistent cell count")
    if used_docling != (docling_source is not None):
        raise ValueError("Docling references and source metadata disagree")


def validate(original, dehydrated, restored, limit=None, start_index=0):
    errors = tables = 0
    sources = (dehydrate.selected_records(original, limit, start_index), hydrator.records_in(dehydrated), hydrator.records_in(restored))
    for position, triple in enumerate(zip_longest(*sources), 1):
        if any(item is None for item in triple):
            print("Error: record counts differ", file=sys.stderr)
            return tables, errors + 1
        (_, source), (_, dry), (_, wet) = triple
        try:
            if (source.get("bulletin_id"), source.get("pair_type")) != (dry.get("bulletin_id"), dry.get("pair_type")) or (
                source.get("bulletin_id"), source.get("pair_type")
            ) != (wet.get("bulletin_id"), wet.get("pair_type")):
                raise ValueError("record identity differs")
            original_table = hydrator.table_container(source)
            if original_table is None:
                continue
            dry_table = hydrator.table_container(dry)
            wet_table = hydrator.table_container(wet)
            if dry_table is None or wet_table is None:
                raise ValueError("table object missing")
            if "table_html" in dry or "table_html" in dry_table:
                raise ValueError("table_html remains in the dehydrated record")
            value = original_table.get("table_html")
            if not isinstance(value, str):
                raise ValueError("original needs hydrated table_html for exact comparison")
            if wet_table.get("table_html") != value:
                raise ValueError("restored table_html does not exactly match the original")
            if "table_cell_reproduction" in dry_table:
                if "table_reproduction" in dry_table:
                    raise ValueError("conflicting table recipes")
                recipe = dry_table["table_cell_reproduction"]
                verify_cell_recipe(recipe)
                if recipe.get("html_sha256") != cells.digest(value):
                    raise ValueError("cell recipe does not match the original HTML digest")
            elif "table_reproduction" in dry_table:
                recipe = dry_table["table_reproduction"]
                allowed = {"version", "method", "pdf_sha256", "html_sha256", "docling_version", "ocr_engine", "ocr_languages"}
                if not isinstance(recipe, dict) or set(recipe) != allowed or recipe.get("method") != "docling_bbox":
                    raise ValueError("invalid strict Docling recipe")
                if recipe.get("html_sha256") != cells.digest(value):
                    raise ValueError("Docling recipe does not match the original HTML digest")
            else:
                raise ValueError("missing verified table recipe")
            tables += 1
        except ValueError as exc:
            errors += 1
            print(f"Record {position}: {exc}", file=sys.stderr)
    if not tables and not errors:
        print("Error: no tables were checked", file=sys.stderr)
        errors += 1
    return tables, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("dehydrated", type=Path)
    parser.add_argument("restored", type=Path)
    parser.add_argument("--limit", type=int, help="Compare only the first N original records")
    parser.add_argument("--start-index", type=int, default=0, help="Skip this many original JSONL records")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.start_index < 0:
        parser.error("--start-index must be non-negative")
    try:
        count, errors = validate(args.original, args.dehydrated, args.restored, args.limit, args.start_index)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Checked {count} tables; errors {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
