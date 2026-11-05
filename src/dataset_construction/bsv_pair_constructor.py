import re

MIN_TEXT_LEN = 50



# Minimum text length to consider a section as having real text
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

