"""Check reproduction-authorization phrases in PDFs referenced by a JSONL file."""

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF_DIRS = (ROOT / "data" / "local" / "pdfs",)

INTEGRAL_PHRASE = "reproduction intégrale de ce bulletin autorisée"
PARTIAL_PHRASE = "reproduction partielle autorisée"

CSV_COLUMNS = (
    "bulletin_id",
    "pdf_path",
    "pdf_found",
    "pdf_read",
    "integral_reproduction_authorized",
    "partial_reproduction_authorized",
    "error",
)


def read_bulletin_ids(path: Path) -> list[str]:
    """Read unique, non-empty bulletin IDs in first-seen order."""
    bulletin_ids = []
    seen = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path} on line {line_number}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected a JSON object in {path} on line {line_number}")

            value = record.get("bulletin_id")
            if value is None or not str(value).strip():
                continue
            bulletin_id = str(value).strip()
            if bulletin_id not in seen:
                seen.add(bulletin_id)
                bulletin_ids.append(bulletin_id)
    return bulletin_ids


def find_pdfs(directories: list[Path], bulletin_ids: set[str]) -> dict[str, Path]:
    """Find PDFs by exact filename stem, preferring earlier directories."""
    result = {}
    for directory in directories:
        if not directory.is_dir():
            print(f"Warning: PDF directory does not exist: {directory}", file=sys.stderr)
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".pdf" and path.stem in bulletin_ids:
                result.setdefault(path.stem, path)
    return result


def normalize_text(text: str) -> str:
    """Normalize case, Unicode, and whitespace for reliable phrase matching."""
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    return re.sub(r"\s+", " ", text).casefold()


def inspect_pdf(pdf_path: Path) -> tuple[bool, bool]:
    """Return whether the PDF contains each authorization phrase."""
    import pymupdf as fitz

    with fitz.open(pdf_path) as document:
        text = "\n".join(page.get_text() for page in document)
    normalized = normalize_text(text)
    return (
        normalize_text(INTEGRAL_PHRASE) in normalized,
        normalize_text(PARTIAL_PHRASE) in normalized,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read unique bulletin_id values from a JSONL file, find the matching PDFs, "
            "and report whether each authorization phrase appears in the PDF text."
        )
    )
    parser.add_argument("input", type=Path, help="Input JSONL file")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV (default: INPUT_STEM_reproduction_rights.csv)",
    )
    parser.add_argument(
        "--pdf-dir",
        action="append",
        type=Path,
        help=(
            "PDF root to search recursively; may be repeated. Defaults to "
            "data/local/pdfs inside the repository."
        ),
    )
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"Input JSONL does not exist: {args.input}")
    output = args.output or args.input.with_name(
        f"{args.input.stem}_reproduction_rights.csv"
    )
    if args.input.resolve() == output.resolve():
        parser.error("Input JSONL and output CSV must be different files")

    try:
        bulletin_ids = read_bulletin_ids(args.input)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    pdf_dirs = args.pdf_dir if args.pdf_dir is not None else list(DEFAULT_PDF_DIRS)
    pdf_by_id = find_pdfs(pdf_dirs, set(bulletin_ids))

    output.parent.mkdir(parents=True, exist_ok=True)
    found_count = 0
    read_count = 0
    with output.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for bulletin_id in bulletin_ids:
            pdf_path = pdf_by_id.get(bulletin_id)
            row = {
                "bulletin_id": bulletin_id,
                "pdf_path": str(pdf_path) if pdf_path else "",
                "pdf_found": bool(pdf_path),
                "pdf_read": False,
                "integral_reproduction_authorized": "",
                "partial_reproduction_authorized": "",
                "error": "",
            }
            if pdf_path is None:
                row["error"] = "PDF not found"
            else:
                found_count += 1
                try:
                    integral, partial = inspect_pdf(pdf_path)
                    row["pdf_read"] = True
                    row["integral_reproduction_authorized"] = integral
                    row["partial_reproduction_authorized"] = partial
                    read_count += 1
                except Exception as exc:
                    row["error"] = str(exc)
            writer.writerow(row)

    print(f"Unique bulletin IDs: {len(bulletin_ids)}")
    print(f"PDFs found: {found_count}; PDFs read: {read_count}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
