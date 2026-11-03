"""
BSV Pair Constructor

Reads bsv_dataset.jsonl and generates multimodal pairs from sections
that have at least two modalities. Ignores sections with only one modality.

Pair types:
    (text, image)  — section text paired with one image from the same section
    (text, table)  — section text paired with one table from the same section
    (image, table) — one image paired with one table from the same section
    (image, caption) — one image paired with its own caption text (when caption exists)


For sections with text + image + table, all three pair types are generated.

Output: bsv_pairs.jsonl — one pair per line.

Each pair record:
    pair_type     : "text_image" | "text_table" | "image_table"
    bulletin_id   : source bulletin
    crop          : crop type (may be None)
    date          : ISO date (may be None)
    year          : year string
    region_code   : Wikidata region code
    section_title : section header
    text          : paragraph text (present for text_image and text_table pairs)
    image         : {filename, page, caption} (present for text_image and image_table pairs)
    table         : {page, rows}              (present for text_table and image_table pairs)
"""

import json
import os
import random
import re


# ---------------------------------------------------------------------------
# Configuration — edit these paths
# ---------------------------------------------------------------------------

# NOTE: paths below are from before the folder reorganization and the
# GT / Automatic Construction split. This file's own main() is 
# superseded by generate_pairs() being imported directly elsewhere.
# for this script to be run standalone,
# update IN_JSONL to the  current raw dataset location
# (e.g. "Automatic Construction/bsv_dataset_auto_textimage.jsonl" or
# "Ground Truth/bsv_dataset_gt_2404.jsonl"), and update the three
# OUT_* paths to a real destination folder.
IN_JSONL   = "/projects/agrifutur/yabusaqe/BSV Sample Construction/TODO_SET_INPUT_PATH/bsv_dataset.jsonl"
OUT_PAIRS  = "/projects/agrifutur/yabusaqe/BSV Sample Construction/TODO_SET_OUTPUT_PATH/bsv_pairs.jsonl"
SAMPLE_OUT_500  = "/projects/agrifutur/yabusaqe/BSV Sample Construction/TODO_SET_OUTPUT_PATH/bsv_pairs_sample_500.jsonl"
SAMPLE_OUT_1000 = "/projects/agrifutur/yabusaqe/BSV Sample Construction/TODO_SET_OUTPUT_PATH/bsv_pairs_sample_1000.jsonl"
SAMPLE_N        = 50
SEED       = 42

# Minimum text length to consider a section as having real text
MIN_TEXT_LEN = 50

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
CHART_NOISE_RE = [
    re.compile(r'FREDON', re.I),
    re.compile(r'Nombre moyen de captures', re.I),
    re.compile(r'par pi[eé]ge', re.I),
    re.compile(r'(?:\d+[,.]?\d*\s+){4,}'),
    re.compile(r'Moyenne hebdomadaire', re.I),
    re.compile(r'Maximum hebdomadaire', re.I),
    re.compile(r'Seuil de nuisibilit', re.I),
]

def has_chart_noise(text):
    return any(p.search(text) for p in CHART_NOISE_RE)
    
def has_text(r):
    return len(r.get("text", "").strip()) >= MIN_TEXT_LEN

def has_images(r):
    return len(r.get("images", [])) > 0

def has_tables(r):
    return len(r.get("tables", [])) > 0

def base_fields(r):
    return {
        "bulletin_id":   r.get("bulletin_id"),
        "crop":          r.get("crop"),
        "date":          r.get("date"),
        "year":          r.get("year"),
        "region_code":   r.get("region_code"),
        "section_title": r.get("section_title"),
    }

# ---------------------------------------------------------------------------
# Pair generation
# ---------------------------------------------------------------------------

def generate_pairs(record):
    """
    Generate all valid pairs from one section record.
    For sections with text + image + table, all three pair types are produced.
    For sections with two modalities, only the corresponding pair is produced.
    """
    if record.get("section_title") == "Document":
        return []
    if has_chart_noise(record.get("text", "")):
        return []
    pairs = []
    t  = has_text(record)
    i  = has_images(record)
    tb = has_tables(record)

    # Need at least two modalities
    if sum([t, i, tb]) < 2:
        return pairs

    base = base_fields(record)

    for img in record.get("images", []):
        local_text = img.get("local_text")
        if local_text:
            pairs.append({
                **base,
                "pair_type": "text_image",
                "text":      local_text,
                "image":     img,
            })

    # One image+caption pair per image that has a real caption
    for img in record.get("images", []):
        cap = (img.get("caption") or "").strip()
        if len(cap) >= 4:
            pairs.append({
                **base,
                "pair_type": "image_caption",
                "text":      cap,
                "image":     img,
            })

    if t and tb:
        for tbl in record["tables"]:
            if tbl["page"] in record.get("pages", []):
                local_text = tbl.get("local_text")
                if local_text:
                    pairs.append({
                        **base,
                        "pair_type": "text_table",
                        "text":      local_text,
                        "table":     tbl,
                    })

    if i and tb:
        # Cross-product of images and tables
        for img in record["images"]:
            for tbl in record["tables"]:
                pairs.append({
                    **base,
                    "pair_type": "image_table",
                    "image":     img,
                    "table":     tbl,
                })

    return pairs

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    counts = {
        "sections_read":       0,
        "sections_skipped":    0,
        "pairs_text_image":    0,
        "pairs_text_table":    0,
        "pairs_image_table":   0,
        "pairs_image_caption": 0,
    }

    all_pairs = []

    with open(IN_JSONL, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            counts["sections_read"] += 1
            pairs = generate_pairs(record)
            if not pairs:
                counts["sections_skipped"] += 1
                continue
            for p in pairs:
                counts[f"pairs_{p['pair_type']}"] += 1
            all_pairs.extend(pairs)

    total_pairs = len(all_pairs)

    # Write all pairs
    with open(OUT_PAIRS, "w", encoding="utf-8") as f:
        for p in all_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Random sample
    """
    random.seed(SEED)
    sample_1000 = random.sample(all_pairs, min(1000, total_pairs))
    with open(SAMPLE_OUT_1000, "w", encoding="utf-8") as f:
        for p in sample_1000:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    sample_500 = random.sample(sample_1000, min(500, len(sample_1000)))
    with open(SAMPLE_OUT_500, "w", encoding="utf-8") as f:
        for p in sample_500:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    """
    """
    import time
    random.seed(time.time())
    sample_50 = random.sample(all_pairs, min(50, total_pairs))
    with open(SAMPLE_OUT_1000, "w", encoding="utf-8") as f:
        for p in sample_50:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    """

    # Summary
    print(f"Sections read       : {counts['sections_read']}")
    print(f"Sections skipped    : {counts['sections_skipped']} (single modality or empty)")
    print(f"Sections with pairs : {counts['sections_read'] - counts['sections_skipped']}")
    print()
    print(f"Pairs generated:")
    print(f"  text + image  : {counts['pairs_text_image']}")
    print(f"  text + table  : {counts['pairs_text_table']}")
    print(f"  image + table : {counts['pairs_image_table']}")
    print(f"  image + caption: {counts['pairs_image_caption']}")
    print(f"  total         : {total_pairs}")
    print()
    print(f"Sample of 1000 pairs saved to: {SAMPLE_OUT_1000}")
    print(f"Sample of 500 pairs saved to : {SAMPLE_OUT_500}")
    print(f"Full pairs saved to          : {OUT_PAIRS}")
    #print(f"Sample of {len(sample)} pairs saved to: {SAMPLE_OUT}")
    print(f"Full pairs saved to: {OUT_PAIRS}")
