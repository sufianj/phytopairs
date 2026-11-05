"""Restore exact JSONL text from PDF character references and literal edits."""

import argparse
from collections import OrderedDict
import json
import os
from pathlib import Path
import sys
import tempfile

try:
    import pymupdf as fitz
except ImportError:
    sys.exit("PyMuPDF is required: python -m pip install 'PyMuPDF>=1.24'")

from locate_jsonl_text import DEFAULT_PDF_DIRS, find_pdfs, records_in, valid_id
from text_reconstruction import file_sha256, page_data, restore_recipe


class PdfTextCache:
    def __init__(self, paths, size=8):
        self.paths = paths
        self.size = size
        self.cache = OrderedDict()

    def get(self, bulletin_id):
        if bulletin_id not in self.cache:
            path = self.paths.get(bulletin_id)
            if path is None:
                raise ValueError(f"PDF not found for bulletin_id {bulletin_id!r}")
            try:
                digest = file_sha256(path)
                with fitz.open(path) as document:
                    if document.is_encrypted:
                        raise ValueError("PDF is encrypted")
                    # Match the producer's get_textpage() flags exactly;
                    # rawdict's implicit flags normalize some spaces differently.
                    pages = [page_data(page, page.get_textpage())[0] for page in document]
                self.cache[bulletin_id] = (digest, pages)
            except Exception as exc:
                self.cache[bulletin_id] = ValueError(f"Cannot read PDF {path}: {exc}")
            if len(self.cache) > self.size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(bulletin_id)
        data = self.cache[bulletin_id]
        if isinstance(data, Exception):
            raise data
        return data


def restore_record(record, pdf_cache):
    bulletin_id = record.get("bulletin_id")
    if not valid_id(bulletin_id):
        raise ValueError("missing or invalid bulletin_id")
    if ("text_reproduction" in record and "text_reconstruction" in record
            and record["text_reproduction"] != record["text_reconstruction"]):
        raise ValueError("conflicting text_reproduction and text_reconstruction recipes")
    recipe = record.get("text_reproduction", record.get("text_reconstruction"))
    if not isinstance(recipe, dict):
        raise ValueError("missing text_reproduction recipe")
    digest, pages = pdf_cache.get(bulletin_id)
    if recipe.get("pdf_sha256") != digest:
        raise ValueError("source PDF SHA-256 does not match the recipe")
    text = restore_recipe(recipe, pages)
    if "text" in record and record["text"] != text:
        raise ValueError("existing text differs from the reconstructed text")
    fields = recipe.get("removed_duplicate_fields", [])
    allowed = {"image.local_text", "table.local_text", "image.caption"}
    if (not isinstance(fields, list) or any(not isinstance(field, str) or field not in allowed for field in fields)
            or len(fields) != len(set(fields))):
        raise ValueError("invalid removed_duplicate_fields")
    for field in fields:
        container, key = field.split(".")
        value = record.get(container)
        if not isinstance(value, dict):
            raise ValueError(f"cannot restore {field}: missing {container} object")
        if key in value and value[key] != text:
            raise ValueError(f"existing {field} differs from reconstructed text")
    record["text"] = text
    for field in fields:
        container, key = field.split(".")
        record[container][key] = text


def process(input_path, output_path, directories, overwrite=False):
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Input and output must be different files")
    if output_path.exists() and not overwrite:
        raise ValueError(f"Output already exists: {output_path}; use --overwrite to replace it")
    if not any(directory.is_dir() for directory in directories):
        paths = ", ".join(str(directory) for directory in directories)
        raise ValueError(f"No PDF directories exist: {paths}. Specify the correct paths with --pdf-dir")
    ids = {
        record.get("bulletin_id") for _, record in records_in(input_path)
        if (isinstance(record.get("text_reproduction"), dict)
            or isinstance(record.get("text_reconstruction"), dict)) and valid_id(record.get("bulletin_id"))
    }
    cache = PdfTextCache(find_pdfs(directories, ids))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    restored = skipped = failures = 0
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as destination:
            temp_path = Path(destination.name)
            for number, record in records_in(input_path):
                try:
                    if ("text_reproduction" not in record and "text_reconstruction" not in record
                            and isinstance(record.get("text"), str)):
                        skipped += 1
                    else:
                        restore_record(record, cache)
                        restored += 1
                except (ValueError, OSError) as exc:
                    failures += 1
                    print(f"Line {number} ({record.get('bulletin_id')!r}): {exc}", file=sys.stderr)
                destination.write(json.dumps(record, ensure_ascii=False) + "\n")
        if overwrite:
            os.replace(temp_path, output_path)
        else:
            os.link(temp_path, output_path)
            temp_path.unlink()
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return restored, skipped, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input JSONL with text_reproduction recipes")
    parser.add_argument("output", type=Path, help="Rehydrated JSONL (must differ from input)")
    parser.add_argument("--pdf-dir", action="append", type=Path, help="PDF root to search recursively; repeatable")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()
    try:
        restored, skipped, failures = process(args.input, args.output, args.pdf_dir or DEFAULT_PDF_DIRS, args.overwrite)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}; restored {restored}; already hydrated {skipped}; failed {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
