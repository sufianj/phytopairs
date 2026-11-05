"""
Corrects table-leak-into-text issue in bsv_pairs_auto_textimage.jsonl.

Reads the ORIGINAL file as input only (never modified). For each unique
PDF, re-extracts section text with real table bboxes excluded (using
pdfplumber's find_tables(), no quality filtering, since here we only
need to know WHERE a table is, not whether it's good enough to be a
pair). Matches corrected text back to each pair by (bulletin_id,
section_title). Drops any pair whose section no longer exists (i.e.
the section was 100% table content, 0% real text).

Checkpointed by bulletin_id, safe to interrupt and resume.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = ROOT / "3.Dataset Construction"
sys.path.insert(0, str(DATASET_ROOT / "Parsers"))
import bsv_parser
import pdfplumber

BASE = DATASET_ROOT
INPUT_FILE      = BASE / "Automatic Construction" / "bsv_pairs_auto_textimage.jsonl"
OUTPUT_FILE     = BASE / "Automatic Construction" / "bsv_pairs_auto_textimage_corrected.jsonl"
CHECKPOINT_FILE = BASE / "Scripts" / "processed_bulletins_table_fix.txt"
PDF_DIRS        = [ROOT / "data" / "local" / "pdfs"]


def find_pdf(bulletin_id):
    for d in PDF_DIRS:
        p = d / (bulletin_id + ".pdf")
        if p.exists():
            return p
    return None


def get_table_bboxes_for_exclusion(pdf_path):
    bboxes_by_page = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            for tbl_obj in page.find_tables():
                bboxes_by_page.setdefault(page_num, []).append(tbl_obj.bbox)
    return bboxes_by_page


def get_corrected_sections(pdf_path):
    table_bboxes = get_table_bboxes_for_exclusion(pdf_path)
    s_raw, i_raw, text_els = bsv_parser.parse_structure(pdf_path, table_bboxes)
    sections = bsv_parser.split_into_sections(
        bsv_parser.extract_text(pdf_path, table_bboxes), s_raw
    )
    # drop sections with no real text left (were 100% table content)
    sections = [s for s in sections if s.get("text", "").strip()]
    return {s["section_title"]: s["text"] for s in sections}


def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    with open(CHECKPOINT_FILE, encoding="utf-8") as f:
        return set(f.read().splitlines())


def append_checkpoint(bulletin_id):
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(bulletin_id + "\n")


def main():
    with INPUT_FILE.open(encoding="utf-8") as f:
        all_pairs = [json.loads(line) for line in f]

    pairs_by_bulletin = {}
    for p in all_pairs:
        pairs_by_bulletin.setdefault(p["bulletin_id"], []).append(p)

    all_bulletins = sorted(pairs_by_bulletin.keys())
    done_bulletins = load_checkpoint()
    remaining = [b for b in all_bulletins if b not in done_bulletins]

    print(f"{len(all_bulletins)} unique bulletins total")
    print(f"{len(done_bulletins)} already processed (resuming)")
    print(f"{len(remaining)} remaining")

    stats = {"kept": 0, "dropped_no_section": 0, "pdf_not_found": 0, "errors": 0}

    out_mode = "a" if done_bulletins else "w"
    with OUTPUT_FILE.open(out_mode, encoding="utf-8") as out_f:
        for i, bulletin_id in enumerate(remaining, 1):
            path = find_pdf(bulletin_id)
            if not path:
                stats["pdf_not_found"] += len(pairs_by_bulletin[bulletin_id])
                append_checkpoint(bulletin_id)
                continue

            try:
                corrected_sections = get_corrected_sections(path)
            except Exception as e:
                print(f"  ERROR on {bulletin_id}: {e}")
                stats["errors"] += len(pairs_by_bulletin[bulletin_id])
                append_checkpoint(bulletin_id)
                continue

            for p in pairs_by_bulletin[bulletin_id]:
                title = p["section_title"]
                if title not in corrected_sections:
                    stats["dropped_no_section"] += 1
                    continue
                new_p = dict(p)
                new_p["text"] = corrected_sections[title]
                out_f.write(json.dumps(new_p, ensure_ascii=False) + "\n")
                stats["kept"] += 1

            out_f.flush()
            append_checkpoint(bulletin_id)

            if i % 100 == 0 or i == len(remaining):
                print(f"[{i}/{len(remaining)}] kept={stats['kept']} "
                      f"dropped={stats['dropped_no_section']} "
                      f"pdf_missing={stats['pdf_not_found']} errors={stats['errors']}")

    print()
    print("Done.")
    print(f"Kept: {stats['kept']}")
    print(f"Dropped (all-table section): {stats['dropped_no_section']}")
    print(f"PDF not found: {stats['pdf_not_found']}")
    print(f"Errors: {stats['errors']}")


if __name__ == "__main__":
    main()
