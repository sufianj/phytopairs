#!/usr/bin/env python3
"""
recall_approx.py — command-line approximation of "meaningful" recall.

WHY THIS EXISTS
-----------------
count_raw_images()/count_raw_tables() (added last night) count EVERY embedded
image object / EVERY pdfplumber-detected table, no filtering at all. That
includes things nobody would call "meaningful": tiny icons, logo banners,
blank near-white pages, thin separator lines. So a recall number against that
raw denominator conflates two very different things:
  (a) real content correctly rejected as noise (fine, working as intended)
  (b) real content incorrectly rejected (an actual problem)

This script does NOT resolve that ambiguity. Only a manual eyeball count can.
What it adds is a second, much looser denominator: images/tables that pass a
generous "could plausibly be real content" filter, well below your actual
production thresholds. Comparing recall against BOTH the raw and the loose
denominator tells you how much of the recall gap survives even a forgiving
filter, which narrows down how worried to be before doing the manual count.

Treat "loose" numbers as an upper-bound sanity check, not ground truth.

USAGE
------
    python3 recall_approx.py /path/to/pdf_folder
    python3 recall_approx.py /path/to/pdf_folder --limit 30
"""

import argparse
import io
import os
import sys
from collections import Counter

import fitz               # PyMuPDF
import pdfplumber
from PIL import Image as PILImage

# Import your real bsv_parser.py so "kept" counts use your ACTUAL production
# heuristics, not a third, possibly-divergent reimplementation of them.
sys.path.insert(0, "/projects/agrifutur/yabusaqe/BSV Sample Construction/Parsers")
try:
    import bsv_parser
    HAVE_PARSER = True
except ImportError:
    HAVE_PARSER = False
    print("WARNING: bsv_parser.py not found next to this script. "
          "Only raw + loose counts will be shown, no 'kept' column.")

if HAVE_PARSER:
    # Mirrors tonight's GT-only batch overrides, so 'kept' reflects the
    # loosened settings, not the untouched strict production thresholds.
    bsv_parser.MIN_IMAGE_AREA = 30000
    os.environ["SKIP_OCR"] = "1"

    _strict_parse_structure = bsv_parser.parse_structure

    def _loose_parse_structure(pdf_path, table_bboxes):
        s_raw, i_raw, text_els = _strict_parse_structure(pdf_path, table_bboxes)
        d = fitz.open(pdf_path)
        total_pages = d.page_count
        if total_pages >= 2:
            page = d[total_pages - 1]
            page_height = page.rect.height
            already_seen = {img["xref"] for img in i_raw}
            info_list = {item["xref"]: item for item in page.get_image_info(hashes=False, xrefs=True)}
            seen_here = set()
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in already_seen or xref in seen_here:
                    continue
                seen_here.add(xref)
                info = info_list.get(xref)
                if not info or not info.get("bbox"):
                    continue
                bbox = info["bbox"]
                top = bbox[1]
                if top >= 0.90 * page_height:
                    continue
                i_raw.append({"page": total_pages, "top": top, "bottom": bbox[3],
                              "left": bbox[0], "right": bbox[2],
                              "xref": xref, "page_height": page_height})
        d.close()
        return s_raw, i_raw, text_els

    bsv_parser.parse_structure = _loose_parse_structure

    def _loose_extract_tables(pdf_path):
        result = []
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                if page_num in bsv_parser.SKIP_PAGES:
                    continue
                for tbl_obj in page.find_tables():
                    table = tbl_obj.extract()
                    if not table or len(table) < 3:
                        continue
                    n_cols = max(len(r) for r in table)
                    if n_cols < 2:
                        continue
                    flat = [c for row in table for c in row if c]
                    avg_len = sum(len(c) for c in flat) / max(len(flat), 1)
                    if avg_len > 200:
                        continue
                    all_cells = [c for row in table for c in row]
                    empty_pct = sum(1 for c in all_cells if not c or str(c).strip() == "") / max(len(all_cells), 1)
                    if empty_pct > 0.92:
                        continue
                    result.append({"page": page_num, "rows": table, "bbox": tbl_obj.bbox})
        result and result
        return result

    bsv_parser.extract_tables = _loose_extract_tables

# ---------------------------------------------------------------------------
# Loose "plausibly meaningful" thresholds. Deliberately far more permissive
# than production (see bsv_parser_subsections_v3.ipynb for the real values).
# The intent is to exclude only near-certain junk, not to judge quality.
# ---------------------------------------------------------------------------
LOOSE_MIN_AREA        = 20_000   # production: 200,000
LOOSE_MAX_ASPECT      = 15.0     # production: 5.5
LOOSE_MAX_NEAR_WHITE  = 0.95     # production: 0.80
LOOSE_MIN_TABLE_ROWS  = 2        # production: 3
LOOSE_MIN_TABLE_COLS  = 2        # production: 2 (same)
LOOSE_MAX_TABLE_EMPTY = 0.80     # production: 0.50


def count_images(pdf_path):
    """Returns (raw_count, loose_meaningful_count) for images in one PDF."""
    doc = fitz.open(pdf_path)
    raw_xrefs = set()
    seen_loose = set()
    loose_count = 0
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            raw_xrefs.add(xref)
            if xref in seen_loose:
                continue
            try:
                img_data = doc.extract_image(xref)
                w, h = img_data["width"], img_data["height"]
                if w * h < LOOSE_MIN_AREA:
                    continue
                if w / max(h, 1) > LOOSE_MAX_ASPECT:
                    continue
                pil_img = PILImage.open(io.BytesIO(img_data["image"])).convert("RGB")
                pixels = list(pil_img.getdata())
                total = len(pixels)
                near_white = sum(
                    1 for r, g, b in pixels if r > 240 and g > 240 and b > 240
                ) / total
                if near_white > LOOSE_MAX_NEAR_WHITE:
                    continue
                seen_loose.add(xref)
                loose_count += 1
            except Exception:
                continue
    doc.close()
    return len(raw_xrefs), loose_count


def count_tables(pdf_path):
    """Returns (raw_count, loose_meaningful_count) for tables in one PDF."""
    raw_count = 0
    loose_count = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for tbl_obj in page.find_tables():
                raw_count += 1
                table = tbl_obj.extract()
                if not table:
                    continue
                if len(table) < LOOSE_MIN_TABLE_ROWS:
                    continue
                if max(len(r) for r in table) < LOOSE_MIN_TABLE_COLS:
                    continue
                all_cells = [c for row in table for c in row]
                empty_pct = sum(
                    1 for c in all_cells if not c or str(c).strip() == ""
                ) / max(len(all_cells), 1)
                if empty_pct > LOOSE_MAX_TABLE_EMPTY:
                    continue
                loose_count += 1
    return raw_count, loose_count


def main():
    parser = argparse.ArgumentParser(
        description="Approximate meaningful image/table recall (loose filter vs. raw vs. kept)."
    )
    parser.add_argument("pdf_dir", help="Folder containing PDFs to scan")
    parser.add_argument("--limit", type=int, default=None, help="Only scan the first N PDFs")
    args = parser.parse_args()

    pdf_files = sorted(f for f in os.listdir(args.pdf_dir) if f.endswith(".pdf"))
    if args.limit:
        pdf_files = pdf_files[: args.limit]

    totals = Counter()

    for fname in pdf_files:
        path = os.path.join(args.pdf_dir, fname)
        try:
            raw_img, loose_img = count_images(path)
            raw_tbl, loose_tbl = count_tables(path)
        except Exception as e:
            print(f"SKIPPED {fname}: {e}")
            continue

        kept_img = kept_tbl = None
        if HAVE_PARSER:
            try:
                records = bsv_parser.parse_pdf(path, "/tmp/recall_approx_imgs", url=None)
                if records:
                    kept_img = records[0].get("images_kept")
                    kept_tbl = records[0].get("tables_kept")
            except Exception:
                pass

        totals["raw_img"] += raw_img
        totals["loose_img"] += loose_img
        totals["raw_tbl"] += raw_tbl
        totals["loose_tbl"] += loose_tbl
        if kept_img is not None:
            totals["kept_img"] += kept_img
            totals["kept_tbl"] += kept_tbl
            totals["n_with_kept"] += 1

        print(
            f"{fname:55s} img: raw={raw_img:3d} loose={loose_img:3d} "
            f"kept={kept_img if kept_img is not None else '?'}   "
            f"tbl: raw={raw_tbl:3d} loose={loose_tbl:3d} "
            f"kept={kept_tbl if kept_tbl is not None else '?'}"
        )

    print("\n" + "=" * 70)
    print(f"PDFs scanned: {len(pdf_files)}")

    print("\nIMAGES")
    print(f"  raw total   : {totals['raw_img']}")
    print(f"  loose total : {totals['loose_img']}")
    if totals.get("n_with_kept"):
        print(f"  kept total  : {totals['kept_img']}")
        print(f"  recall vs raw   : {100 * totals['kept_img'] / max(totals['raw_img'], 1):.1f}%")
        print(f"  recall vs loose : {100 * totals['kept_img'] / max(totals['loose_img'], 1):.1f}%")

    print("\nTABLES")
    print(f"  raw total   : {totals['raw_tbl']}")
    print(f"  loose total : {totals['loose_tbl']}")
    if totals.get("n_with_kept"):
        print(f"  kept total  : {totals['kept_tbl']}")
        print(f"  recall vs raw   : {100 * totals['kept_tbl'] / max(totals['raw_tbl'], 1):.1f}%")
        print(f"  recall vs loose : {100 * totals['kept_tbl'] / max(totals['loose_tbl'], 1):.1f}%")

    print(
        "\nReminder: 'loose' is a generous approximation of 'plausibly real "
        "content', not ground truth. It will still misjudge some borderline "
        "cases either way. A manual eyeball count on a small sample is the "
        "only way to know the true number."
    )


if __name__ == "__main__":
    main()
