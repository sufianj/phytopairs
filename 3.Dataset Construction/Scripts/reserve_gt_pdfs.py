"""
reserve_gt_pdfs.py

Randomly reserves a fixed set of PDFs for GT construction, drawn from BOTH
source folders combined, and writes a manifest listing which ones (filename
+ source folder). This manifest is the reservation record, used later to:
  - build the GT-specific (loosened-settings) batch input list
  - exclude these exact filenames from the automatic 20k-corpus construction
    run, per the "never reuse GT PDFs for auto-construction" rule

This script does NOT copy, move, rename, or write anything into the shared
PDF folders. It only reads filenames from them and writes the manifest into
your own project folder.

Usage:
    python3 reserve_gt_pdfs.py
"""

import os
import random
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF_DIR = ROOT / "data" / "local" / "pdfs"
N_RESERVE = 3205
SEED = 42  # fixed seed so this exact selection is reproducible if regenerated
OUT_MANIFEST = ROOT / "3.Dataset Construction" / "Ground Truth" / "gt_reserved_pdfs.jsonl"


def list_pdfs(directory):
    return sorted(f for f in os.listdir(directory) if f.endswith(".pdf"))


def manifest_directory(directory):
    """Store repository directories relative to the repository root."""
    path = Path(directory)
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def main():
    parser = argparse.ArgumentParser(description="Reserve source PDFs for manual test-set curation.")
    parser.add_argument(
        "--pdf-dir", action="append", type=Path, default=None,
        help="PDF directory (repeatable); defaults to data/local/pdfs.",
    )
    parser.add_argument("--output", type=Path, default=OUT_MANIFEST)
    args = parser.parse_args()

    pdf_dirs = args.pdf_dir if args.pdf_dir is not None else [DEFAULT_PDF_DIR]
    combined = []
    for directory in pdf_dirs:
        files = list_pdfs(directory)
        print(f"{directory}: {len(files)} PDFs")
        combined.extend((filename, str(directory)) for filename in files)
    by_filename = {}
    for filename, directory in combined:
        if filename in by_filename:
            raise ValueError(f"Duplicate PDF filename across source directories: {filename}")
        by_filename[filename] = directory
    combined = list(by_filename.items())
    total = len(combined)
    print(f"Combined pool: {total} PDFs")

    if N_RESERVE > total:
        raise ValueError(f"Requested {N_RESERVE} PDFs but only {total} exist in total.")

    random.seed(SEED)
    shuffled = combined[:]
    random.shuffle(shuffled)
    reserved = shuffled[:N_RESERVE]

    print(f"Reserved {len(reserved)} PDFs for GT across {len(pdf_dirs)} source directory/ies")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for fname, directory in reserved:
            f.write(json.dumps({"filename": fname, "source_dir": manifest_directory(directory)}) + "\n")

    print(f"\nManifest written to: {args.output}")
    print("This manifest is the reservation record only, no PDF was moved or copied.")
    print("Use it to (a) build the GT batch's input list, and (b) exclude these exact "
          "filenames from the automatic 20k-corpus construction run.")


if __name__ == "__main__":
    main()
