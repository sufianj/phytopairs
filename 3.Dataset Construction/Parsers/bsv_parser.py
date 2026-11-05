import os, re, shutil
from pathlib import Path
import fitz          # pymupdf
import pdfplumber
from PIL import Image as PILImage
import io


KNOWN_CROPS = [
    "vigne","ble","mais","colza","tournesol","orge","soja","pois",
    "luzerne","lin","betterave","pomme","poire","peche","cerise",
    "fraise","framboise","carotte","tomate","laitue","chou","poireau",
    "oignon","ail","melon","concombre","courgette","tabac","houblon",
    "noix","noisette","lavande","chanvre","sorgho","triticale","avoine",
    "prune","abricot","kiwi","cassis","groseille","endive","mache",
    "epinard","poivron","aubergine","asperge","artichaut","haricot",
]

SIZE_THRESHOLD  = 10   # font size (pt) threshold for section headers
LEFT_THRESHOLD  = 15   # min x position to exclude left-edge artifacts
MAX_HEADER_LEN  = 80   # max chars for a section title
SKIP_PAGES      = {1}  # skip cover page
MIN_IMAGE_BYTES = 5000 # filter logos and tiny icons
MIN_IMAGE_AREA  = 200_000

FOOTER_PATTERNS = [
    re.compile(r'bulletin de sant',    re.I),
    re.compile(r'^\d+/\d+\s*$'),
    re.compile(r'^page\s+\d+',         re.I),
    re.compile(r'[–—]\s*N°\s*\d+',    re.I),   # "– N°02" edition header strings
    re.compile(r'N°\s*\d+\s+du\s+\d', re.I),   # "N°02 du 5 mai" date-stamped lines
    re.compile(r'reproduction.*integralit', re.I),
    re.compile(r'^BSV\b', re.I),
    re.compile(r'bulletin.{0,30}responsabilit', re.I),
    re.compile(r'chambre.{0,20}agriculture.{0,20}r.gion', re.I),
    re.compile(r'–\s*(Janvier|Février|Mars|Avril|Mai|Juin|Juillet|Août|Septembre|Octobre|Novembre|Décembre)\s*\d{4}', re.I),
    re.compile(r'\bS\d{2}\b.*\bS\d{2}\b', re.I),  # week references like S51 S50 S49
    re.compile(r'Ce bulletin est produit', re.I),
    re.compile(r'13 avenue|avenue des Droits', re.I),
    re.compile(r'FREDON', re.I),
    re.compile(r'Nombre moyen de captures', re.I),
    re.compile(r'captures hebdomadaires', re.I),
    re.compile(r'par pi[eé]ge', re.I),
    re.compile(r'reproduction.{0,20}(seulement|autoris).{0,30}int[ée]gralit', re.I),
    re.compile(r'reproduction partielle interdite', re.I),
    re.compile(r'^Bulletin n°\d+', re.I),
    re.compile(r'^N°\s*\d+\s*$'),
    re.compile(r'^du\s*$'),
    re.compile(r'Directeur de publication', re.I),
    re.compile(r'd[ée]gage donc toute responsabilit', re.I),
    re.compile(r'Action pilot[ée]e par les Minist', re.I),
    re.compile(r'agence fran[çc]aise de la biodiversit', re.I),
    re.compile(r'redevance pour\s*pollution', re.I),
    re.compile(r'plan Ecophyto', re.I),
    re.compile(r'^Prochain BSV', re.I),
    re.compile(r'^R\s*$'),
    re.compile(r'^EPRODUCTION DU BULLETIN', re.I),
    re.compile(r'^\(\s*$'),
    re.compile(r'^REPRODUCTION PARTIELLE INTERDITE\s*\)?', re.I),
    re.compile(r'a [ée]t[ée] [ée]labor[ée] sur la base des observations', re.I),
    re.compile(r'peut pas [êe]tre transpos[ée]e telle quelle', re.I),
    re.compile(r's.?appuyant sur les pr[ée]conisations issues', re.I),
    re.compile(r'Vous abonner', re.I),
    re.compile(r'Devenir observateur', re.I),
    re.compile(r'(vous\s+abonner|devenir\s+observateur|observateur\s*&\s*contact)', re.I),

]
BAD_HEADER_PATTERNS = [
    re.compile(r'consultez', re.I),
    re.compile(r'guide de l.observateur', re.I),
    re.compile(r'^[«»\s]+$'),
    re.compile(r'^n°\d+', re.I),
    re.compile(r'^edition\b', re.I),
    re.compile(r'^\d{2}/\d{2}/\d{4}$'),
    re.compile(r'crédit photo', re.I),
    re.compile(r'^ce qu.il faut retenir', re.I),
    re.compile(r'^[a-zàâäéèêëîïôùûüç\-]+$'),
    re.compile(r'^\d{1,2}\s+\w+\s+\d{4}', re.I),   # date strings like "16 mai 2017 N°06"
    re.compile(r'^[-+]?\d+[,.]?\d*\s*(°[CcFf]?|%|mm|cm|ha|kg|g)?\s*$'),
    re.compile(r'°[CF]?\s*$',              re.I),   # anything ending with a degree symbol
    re.compile(r'^(Situation|Observations|Contexte|Rappel|Note|Bilan|Attention|Important)\s*$', re.I),
    re.compile(r'^(Fin |Début |Dès |En |À partir |A partir |Durant |Pendant |Depuis )', re.I),
    re.compile(r'^(Très\s+fort|Très\s+faible|Faible|Modéré|Élevé|Fort|Nul|Absent)\s*$', re.I),
    re.compile(r'ressources pour plus d.informations', re.I),
    re.compile(r'pour plus d.informations', re.I),
    re.compile(r'pour en savoir plus', re.I),
    re.compile(r'^(quelques\s+)?(ressources?|informations?|liens?|sites?)\s+(pour|utiles?)', re.I),
    re.compile(r'pour (plus d|en savoir plus|aller plus loin)', re.I),
]

CAPTION_FOOTER_PATTERNS = [p for p in FOOTER_PATTERNS 
                          if 'Janvier' not in p.pattern]

def is_footer(text):  return any(p.search(text) for p in FOOTER_PATTERNS)
def is_bad_header(t): return any(p.search(t)    for p in BAD_HEADER_PATTERNS)
CREDIT_PATTERNS = [
    re.compile(r'^\(?\s*Créd[i]?ts?\s+[Pp]hoto', re.I),
    re.compile(r'^\(?\s*[Pp]hoto\s*[©:\-]',       re.I),
    re.compile(r'^\(?\s*©\s*\w+'),
    re.compile(r'^\(?\s*Source\s*:',               re.I),
]
def is_credit_line(text):
    return len(text.strip()) <= 80 and any(p.match(text.strip()) for p in CREDIT_PATTERNS)
def is_chart_noise(text):
    tokens = text.strip().split()
    if len(tokens) < 3:
        return False
    noisy = sum(1 for t in tokens
                if re.match(r'^[\d,.\-/]+$', t)
                or re.match(r'^\d{1,2}-\w{3,4}$', t)
                or len(t) <= 2)
    if noisy / len(tokens) > 0.5:
        return True
    # reject lines that are purely numeric with no real words
    real_words = sum(1 for t in tokens if re.match(r'^[a-zA-ZÀ-ÿ]{3,}$', t))
    if real_words <= 2 and len(tokens) >= 6:
        return True
    # reject lines mixing sentence text with chart organization labels
    if re.search(r'FREDON|Nombre moyen|par pi[eé]ge|hebdomadaires', text, re.I):
        return True
    return False

def extract_metadata(pdf_path, url=None):
    if url:
        name = Path(url).stem.lower()
    else:
        name = Path(pdf_path).stem.lower()

    meta = {"bulletin_id": Path(url).stem if url else Path(pdf_path).stem, "url": url or ""}

    # Year
    meta["year"] = None
    if url:
        m = re.search(r'/(20\d{2})/', url)
        if m: meta["year"] = m.group(1)
    if not meta["year"]:
        m = re.search(r'(20\d{2})', name)
        if m: meta["year"] = m.group(1)

    # Date
    meta["date"] = None

    # YYYYMMDD — only match if starts with 20
    m = re.search(r'(20\d{6})', name)
    if m:
        d = m.group(1)
        meta["date"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    # DDMMYYYY — 8 digits where last 4 form a valid year
    if not meta["date"]:
        m = re.search(r'(\d{2})(\d{2})(20\d{2})', name)
        if m:
            day, month, year = m.group(1), m.group(2), m.group(3)
            if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
                meta["date"] = f"{year}-{month}-{day}"

    # YYYY-MM-DD or YYYY_MM_DD
    if not meta["date"]:
        m = re.search(r'(20\d{2})[_-](\d{2})[_-](\d{2})', name)
        if m:
            year, month, day = m.group(1), m.group(2), m.group(3)
            meta["date"] = f"{year}-{month}-{day}"

    # DD-MM-YY or DD_MM_YY
    if not meta["date"]:
        m = re.search(r'(?<!\d)(\d{2})[_-](\d{2})[_-](\d{2,4})(?!\d)', name)
        if m:
            day, month, year = m.group(1), m.group(2), m.group(3)
            if len(year) == 2:
                year = "20" + year
            if 1 <= int(day) <= 31 and 1 <= int(month) <= 12 and 2000 <= int(year) <= 2030:
                meta["date"] = f"{year}-{month}-{day}"

    # Crop
    CROP_CATEGORIES = {
        "grandes_cultures": "Grandes Cultures",
        "maraichage":       "Maraîchage",
        "viti":             "Viticulture",
        "vigne":            "Viticulture",
        "arboriculture":    "Arboriculture",
        "arbo":             "Arboriculture",
        "legume":           "Légumes",
        "lin":              "Lin",
        "colza":            "Colza",
        "tournesol":        "Tournesol",
        "pomme_de_terre":   "Pomme de terre",
        "pommedeterre":     "Pomme de terre",
        "pdt":              "Pomme de terre",
        "jevi":             "JEVI",
        "zna":              "ZNA",
        "canne":            "Canne à sucre",
        "olivier":          "Olivier",
        "cereale":          "Céréales",
        "mais":             "Maïs",
        "tabac":            "Tabac",
    }
    meta["crop"] = next((CROP_CATEGORIES[k] for k in CROP_CATEGORIES if k in name), None)

    # Region code
    meta["region_code"] = None
    if url:
        m = re.search(r'/pdf/(Q\d+)/', url)
        if m: meta["region_code"] = m.group(1)

    return meta


def parse_structure(pdf_path, table_bboxes=None):
    table_bboxes = table_bboxes or {}
    from collections import Counter

    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    all_text_els, images_raw_all, xref_page_count = [], [], {}

   # collect page 1 xrefs to exclude from all pages for multi-page bulletins
    page1_xrefs = set()
    if total_pages >= 2:
        for img in doc[0].get_images(full=True):
            page1_xrefs.add(img[0])

    for page_num, page in enumerate(doc, 1):
        if total_pages == 1:
            break  # skip all images from 1-page bulletins
        if page_num == 1 and total_pages >= 2: continue
        if page_num == total_pages: continue  # skip images on the final page
        seen_xrefs = set()
        img_info_list = {item["xref"]: item for item in page.get_image_info(hashes=False, xrefs=True)}
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in page1_xrefs:
                continue
            if xref not in seen_xrefs:
                xref_page_count[xref] = xref_page_count.get(xref, 0) + 1
                seen_xrefs.add(xref)
            info = img_info_list.get(xref)
            if not info or not info.get("bbox"):
                continue
            bbox = info["bbox"]
            images_raw_all.append({
                "page":        page_num,
                "top":         bbox[1],
                "bottom":      bbox[3],
                "left":        bbox[0],
                "right":       bbox[2],
                "xref":        xref,
                "page_height": page.rect.height,
            })

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text or is_footer(text):
                        continue
                    x0    = span["bbox"][0]
                    y0    = span["bbox"][1]
                    size  = span["size"]
                    bold   = bool(span["flags"] & 16) or "Bold"   in span.get("font", "")
                    italic = bool(span["flags"] & 2)  or "Italic" in span.get("font", "")
                    color  = span.get("color", 0)
                    all_text_els.append({"page": page_num, "top": y0, "left": x0,
                     "width": span["bbox"][2] - span["bbox"][0],
                     "size": size, "text": text, "bold": bold,
                     "italic": italic, "color": color})
   

    # Filter recurring logos and deduplicate xrefs
    seen, images_raw = set(), []
    for img in images_raw_all:
        if xref_page_count.get(img["xref"], 0) <= 1 and img["xref"] not in seen:
            seen.add(img["xref"])
            images_raw.append(img)

    # Skip page 1 only for longer bulletins
    skip_pages = {1} if total_pages > 2 else set()

    # Exclude decorative cover-page titles (rare very-large sizes)
    content_bold_sizes = [
        el["size"] for el in all_text_els
        if el["page"] not in skip_pages and el["bold"] and el["size"] >= 10
    ]
    excluded_sizes = set()
    if content_bold_sizes:
        size_counts  = Counter(content_bold_sizes)
        unique_sizes = sorted(size_counts.keys(), reverse=True)
        for i, size in enumerate(unique_sizes):
            if (i + 1 < len(unique_sizes)
                    and size_counts[size] <= 2
                    and size > 1.3 * unique_sizes[i + 1]):
                excluded_sizes.add(size)
            else:
                break

    # Collect ALL bold candidates (no single size threshold)
    # Added: text must not end with punctuation (rules out bold body emphasis)
    # Added: color stored for level assignment
    PUNCT_ENDS = set('.,;!?')
    # build a set of (page, rounded_top) where non-bold text also exists
    # used to reject bold spans that share their line with other text
    mixed_lines = set()
    for el in all_text_els:
        if not el["bold"] and len(el["text"].strip()) > 1:
            mixed_lines.add((el["page"], round(el["top"])))


    # H82: exclude bold text that repeats at the start of more than one page
    # (running headers/titles like "Grandes Cultures n°13 du 10/05/22")
    page_start_zone = 40
    recurring_text_counts = {}
    for el in all_text_els:
        if el["bold"] and el["top"] <= page_start_zone:
            key = el["text"].strip().lower()
            recurring_text_counts[key] = recurring_text_counts.get(key, 0) + 1
    recurring_titles = {k for k, v in recurring_text_counts.items() if v > 1}

    candidates = [
        {"page": el["page"], "top": el["top"], "left": el["left"],
         "text": el["text"], "size": el["size"], "color": el["color"]}
        for el in all_text_els
        if (el["page"] not in skip_pages
            and el["bold"]
            and el["size"] >= SIZE_THRESHOLD - 0.5
            and el["size"] not in excluded_sizes
            and el["left"] >= LEFT_THRESHOLD
            and len(el["text"]) < MAX_HEADER_LEN
            and len(el["text"].strip()) >= 3
            and el["text"].strip()[-1] not in PUNCT_ENDS
            and not is_bad_header(el["text"])
            and not el["text"].strip() == el["text"].strip().lower()
            and not any(
                abs(round(el["top"]) - t) <= 3
                for (p, t) in mixed_lines
                if p == el["page"]
            )
            and not any(
                (bx0 - 5) <= el["left"] <= (bx1 + 5) 
                and (by0 - 5) <= el["top"] <= (by1 + 5)
                for bx0, by0, bx1, by1 in table_bboxes.get(el["page"], [])
            ))
    ]

    # build per-page sorted list of all text top positions for spacing check
    page_tops = {}
    for el in all_text_els:
        page_tops.setdefault(el["page"], []).append(el["top"])
    for p in page_tops:
        page_tops[p].sort()

    def has_adequate_spacing_above(page, top, min_gap=12):
        tops = page_tops.get(page, [])
        above = [t for t in tops if t < top - 1]
        if not above:
            return True
        closest_above = max(above)
        return (top - closest_above) >= min_gap

    candidates = [c for c in candidates if has_adequate_spacing_above(c["page"], c["top"])]

    # CHANGED: assign section levels using color + size
   # --- Level assignment: adapted from pdf2blocs guess_structure ---

    def color_brightness(color):
        r = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        b = color & 0xFF
        return 0.299 * r + 0.587 * g + 0.114 * b

    for c in candidates:
        c["is_light"] = color_brightness(c["color"]) > 200

    # Step 1: adjusted size score per unique (size, is_light) type.
    # is_light gets +0.5 (equivalent to p2b's has_style boost).
    # Rare sizes get a larger boost; frequent sizes get less
    # (equivalent to p2b's nb_blocks frequency adjustment).
    size_counts = Counter(c["size"] for c in candidates)

    def _adjusted_score(size, is_light, count):
        sz = float(size)
        if is_light:
            sz += 0.5
        sz += 0.45 - min(0.45, 0.1 * float(round(count / 3)))
        return sz

    unique_types = sorted(
        set((c["size"], c["is_light"]) for c in candidates),
        key=lambda t: _adjusted_score(t[0], t[1], size_counts[t[0]]),
        reverse=True
    )
    type_to_depth = {t: i for i, t in enumerate(unique_types)}  # 0-indexed

    for c in candidates:
        c["_depth"] = type_to_depth[(c["size"], c["is_light"])]

    # Step 2: succession analysis (p2b's TITLE_OPTIMISE_LEVEL).
    # Group consecutive candidates of the same type, identical to p2b grouping
    # consecutive same-font blocks into title_blocks.
    sorted_cands = sorted(candidates, key=lambda x: (x["page"], x["top"]))
    depth_sequence = []
    prev_type = None
    for c in sorted_cands:
        ctype = (c["size"], c["is_light"])
        if ctype != prev_type:
            depth_sequence.append(type_to_depth[ctype])
            prev_type = ctype

    # For each depth d, record which depths directly precede it.
    n_depths = len(unique_types)
    preced = [[] for _ in range(n_depths)]
    for d, p in zip(depth_sequence[1:], depth_sequence[:-1]):
        if d > p and p not in preced[d]:
            preced[d].append(p)

    # Compute actual depth from precedence (p2b's title_font_depth).
    actual_depth = []
    for i in range(n_depths):
        p = preced[i]
        if len(p) > 0:
            actual_depth.append(max(actual_depth[d] for d in p) + 1)
        else:
            actual_depth.append(0 if len(actual_depth) == 0 else 1)

    # Step 3: assign final section_level (1-indexed, capped at 4).
    for c in candidates:
        d = c["_depth"]
        c["section_level"] = min(actual_depth[d] + 1, 4)
        del c["_depth"]

    # Merge spans on the same header line (within 8px vertically)
    sections_raw = []
    for c in sorted(candidates, key=lambda x: (x["page"], x["top"])):
        if (sections_raw
                and sections_raw[-1]["page"] == c["page"]
                and abs(sections_raw[-1]["top"] - c["top"]) <= 8):
            sections_raw[-1]["text"] += " " + c["text"]
        else:
            sections_raw.append(c)
    for s in sections_raw:
        first_token = s["text"].strip().split()[0] if s["text"].strip() else ""
        if len(first_token) <= 3:
            prefix_candidates = [
                el for el in all_text_els
                if el["page"] == s["page"]
                and el["bold"]
                and abs(el["top"] - s["top"]) <= 8
                and el["left"] < s["left"]
                and len(el["text"].strip()) <= 3
            ]
            if prefix_candidates:
                prefix = max(prefix_candidates, key=lambda x: x["left"])
                s["text"] = prefix["text"].strip() + s["text"].strip()
        # remove bold spans sitting directly above an image (likely bold captions)
    sections_raw = [
        sec for sec in sections_raw
        if not any(
            sec["page"] == img["page"]
            and img["top"] >= sec["top"]
            and img["top"] <= sec["top"] + sec.get("size", 12) + 15
            for img in images_raw
        )
    ]
  # drop capital recovery using easyocr for headers missing their first letter
    try:
        with (Path(__file__).resolve().parent / "french_words.txt").open(encoding="utf-8") as _f:
            _french_words = set(w.strip().lower() for w in _f if w.strip())
    except Exception:
        _french_words = set()

    import os as _os
    if _french_words and not _os.environ.get("SKIP_OCR"):
        import easyocr as _easyocr
        import numpy as _np
        _reader = _easyocr.Reader(['fr'], verbose=False)
        for sec in sections_raw:
            title = sec["text"].strip()
            if not title or title.lower() in _french_words:
                continue
            # only attempt OCR on short single-word titles with no punctuation
            # these are the only realistic drop capital cases
            if " " in title or title.endswith(":") or len(title) > 20:
                continue
            print(f"Trying OCR for: {title!r}")
            page = doc[sec["page"] - 1]
            clip = fitz.Rect(max(0, sec["left"] - 40), sec["top"] - 5, sec["left"], sec["top"] + 20)
            mat = fitz.Matrix(3, 3)
            pix = page.get_pixmap(matrix=mat, clip=clip)
            img_array = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(pix.height, pix.width, 3)
            results = _reader.readtext(img_array)
            print(f"OCR results: {results}")
            for _, text, conf in results:
                text = text.strip()
                if len(text) == 1 and text.isupper() and conf > 0.8:
                    candidate = text + title
                    if candidate.lower() in _french_words:
                        sec["text"] = candidate
                        print(f"Recovered: {candidate}")
                        break

    doc.close()
    return sections_raw, images_raw, all_text_els

def extract_text(pdf_path, table_bboxes=None):
    table_bboxes = table_bboxes or {}
    doc   = fitz.open(pdf_path)
    pages = []
    for page_num, page in enumerate(doc, 1):
        bboxes = table_bboxes.get(page_num, [])
        blocks = page.get_text("dict")["blocks"]
        lines  = []
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text:
                        continue
                    if is_footer(text) or is_credit_line(text) or is_chart_noise(text):
                        continue
                    x0, y0 = span["bbox"][0], span["bbox"][1]
                    margin = 5
                    in_table = any(
                        (bx0 - margin) <= x0 <= (bx1 + margin)
                        and (by0 - margin) <= y0 <= (by1 + margin)
                        for bx0, by0, bx1, by1 in bboxes
                    )
                    if in_table:
                        continue
                    lines.append(text)
        pages.append("\n".join(lines).strip())
    doc.close()
    return pages

def split_into_sections(page_texts, sections_raw):
    seen, ordered_headers = set(), []
    for s in sorted(sections_raw, key=lambda x: (x["page"], x["top"])):
        key = s["text"].strip().lower()
        if key not in seen:
            seen.add(key)
            ordered_headers.append(s)

    PAGE_SEP  = "\n\n<<<PAGE_{page}>>>\n\n"
    full_text = "".join(PAGE_SEP.format(page=i+1) + pt
                        for i, pt in enumerate(page_texts))

    splits = []
    for h in ordered_headers:
        title       = h["text"].strip()
        marker      = f"<<<PAGE_{h['page']}>>>"
        marker_pos  = full_text.find(marker)
        search_from = marker_pos if marker_pos != -1 else 0

        idx = full_text.find(title, search_from)
        if idx == -1:
            for prefix in ["\uf0b7 ", "\uf0fc ", "• ", "- "]:
                idx = full_text.find(prefix + title, search_from)
                if idx != -1:
                    idx += len(prefix)
                    break
        if idx == -1:
            # title may be split across lines in extracted text
            # try matching just the first segment before a dash or newline
            short = re.split(r'\s*[\u2013\u2014\n(]\s*|\s+-\s+', title)[0].strip()
            if len(short) >= 5:
                idx = full_text.find(short, search_from)
                if idx == -1:
                    idx = full_text.lower().find(short.lower(), search_from)
        if idx != -1:
            splits.append((idx, title, h["page"], h["top"], h.get("section_level", 1)))

    splits.sort(key=lambda x: x[0])
    if not splits:
        return [{"section_index": 0, "section_title": "Document",
         "section_level": 1,
         "pages": list(range(1, len(page_texts) + 1)),
         "header_top": 0, "text": re.sub(r'<<<PAGE_\d+>>>', '', full_text).strip()}]

    sections = []
    for i, (pos, title, page, top, level) in enumerate(splits):
        end   = splits[i+1][0] if i+1 < len(splits) else len(full_text)
        chunk = re.sub(r'<<<PAGE_\d+>>>', '', full_text[pos:end]).strip()
        if chunk.lower().startswith(title.lower()):
            chunk = chunk[len(title):].strip()
        sections.append({"section_index": i, "section_title": title,
                  "section_level": level, "pages": [page], 
                  "header_top": top, "text": chunk})
    return sections


def extract_images(pdf_path, out_img_dir, bulletin_id, images_raw, text_els=None):
    text_els = text_els or []
    all_text_els_for_page = {}
    for el in text_els:
        all_text_els_for_page.setdefault(el["page"], []).append(el)
    os.makedirs(out_img_dir, exist_ok=True)
    doc, result, seen = fitz.open(pdf_path), [], set()
    for img_info in images_raw:
        xref = img_info["xref"]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            img_data = doc.extract_image(xref)
        except Exception:
            continue
        if len(img_data["image"]) < MIN_IMAGE_BYTES:
            continue
        if len(img_data["image"]) < MIN_IMAGE_BYTES:
            continue
        if img_data["width"] * img_data["height"] < MIN_IMAGE_AREA:
            continue

        if img_data["width"] / max(img_data["height"], 1) > 5.5:
            continue  # separator strip or footer banner
        # H82: skip images with significant text overlap (likely background layers)
        img_area = (img_info.get("right", 0) - img_info.get("left", 0)) * \
                   (img_info.get("bottom", 0) - img_info.get("top", 0))
        if img_area > 0:
            overlap_area = 0
            for el in all_text_els_for_page.get(img_info["page"], []):
                ex0, ey0 = el["left"], el["top"]
                ex1, ey1 = el["left"] + el.get("width", 0), el["top"] + el.get("size", 10)
                ix0 = max(img_info.get("left", 0), ex0)
                iy0 = max(img_info.get("top", 0), ey0)
                ix1 = min(img_info.get("right", 0), ex1)
                iy1 = min(img_info.get("bottom", 0), ey1)
                if ix1 > ix0 and iy1 > iy0:
                    overlap_area += (ix1 - ix0) * (iy1 - iy0)
            if overlap_area / img_area > 0.15:
                continue

        # H81: skip background images covering most of the page
        page_w = doc[img_info["page"] - 1].rect.width
        page_h = doc[img_info["page"] - 1].rect.height
        img_w  = img_info.get("right", 0) - img_info.get("left", 0)
        img_h  = img_info.get("bottom", 0) - img_info.get("top", 0)
        if page_w > 0 and page_h > 0:
            coverage = (img_w * img_h) / (page_w * page_h)
            if coverage > 0.40:
                continue

        pil_img = PILImage.open(io.BytesIO(img_data["image"])).convert("RGB")
        pixels = list(pil_img.getdata())
        total = len(pixels)
        
        # H36: skip images where more than 80% of pixels are near-white
        near_white = sum(1 for r, g, b in pixels if r > 240 and g > 240 and b > 240)
        if near_white / total > 0.80:
            continue

        near_black = sum(1 for r, g, b in pixels if r < 15 and g < 15 and b < 15)
        if near_black / total > 0.80:
            continue
        
        # H37: skip images where a single color dominates more than 70%
        from collections import Counter
        quantized = pil_img.quantize(colors=8)
        color_counts = Counter(quantized.getdata())
        if max(color_counts.values()) / total > 0.70:
            continue
        new_name = f"{bulletin_id}_p{img_info['page']}_{xref}.{img_data['ext']}"
        with open(os.path.join(out_img_dir, new_name), "wb") as f:
            f.write(img_data["image"])
        result.append({
            "filename": new_name,
            "page":     img_info["page"],
            "top":      img_info["top"],
            "bottom":   img_info.get("bottom", img_info["top"] + 50),
            "left":     img_info.get("left",   0),
            "right":    img_info.get("right",  1000),
            "caption":  None,
        })
    doc.close()
    return result


def match_captions(images, all_text_els):
    page_start_zone = 40
    recurring_text_counts = {}
    for el in all_text_els:
        if el.get("bold") and el["top"] <= page_start_zone:
            key = el["text"].strip().lower()
            recurring_text_counts[key] = recurring_text_counts.get(key, 0) + 1
    recurring_titles = {k for k, v in recurring_text_counts.items() if v > 1}

    for img in images:
        img_bottom = img.get("bottom", img["top"] + 50)
        img_left   = img.get("left",   0)
        img_right  = img.get("right",  1000)

        # find the first qualifying italic span below the image
        trigger = None
        for el in sorted(all_text_els, key=lambda x: x["top"]):
            if el["page"]  != img["page"]:
                continue
            if img["top"] <= el["top"] <= img_bottom:
                continue  # text overlaid inside the image, skip
            if not (img_bottom <= el["top"] <= img_bottom + 15):
                continue
            if not (img_left - 10 <= el["left"] <= img_right + 10):
                continue
            if not el.get("italic"):
                continue
            text = el["text"].strip()
            if not (4 <= len(text) <= 200):
                continue
            if any(p.search(text) for p in CAPTION_FOOTER_PATTERNS) or is_credit_line(text) or is_bad_header(text):
                continue

            # reject if non-italic text exists both before and after on the same line
            same_line = [x for x in all_text_els
                        if x["page"] == el["page"]
                        and abs(x["top"] - el["top"]) <= 3
                        and not x.get("italic")]
            has_before = any(x["left"] < el["left"] for x in same_line)
            has_after  = any(x["left"] > el["left"] + el.get("width", 50) for x in same_line)
            if has_before and has_after:
                continue

            trigger = el
            break

        if trigger is None:
            img["caption"] = None
            continue

        # look for a title line directly above the italic trigger (within 40px)
        above = sorted(
            [el for el in all_text_els
             if el["page"] == trigger["page"]
             and max(trigger["top"] - 40, img_bottom) <= el["top"] < trigger["top"]
             and el["left"] >= img_left - 10
             and el["left"] <= img_right + 10
             and not el.get("bold")
             and not is_footer(el["text"])
             and not is_bad_header(el["text"])
             and el["text"].strip().lower() not in recurring_titles
             and not is_credit_line(el["text"])
             and 4 <= len(el["text"].strip()) <= 200],
            key=lambda x: x["top"],
            reverse=True
        )
        above_text = above[0]["text"].strip() if above else None

        # collect spans on the same line that are contiguous with the trigger
        # stop when horizontal gap exceeds 20pt (indicates a separate caption)
        all_line_spans = sorted(
            [el for el in all_text_els
             if el["page"] == trigger["page"]
             and abs(el["top"] - trigger["top"]) <= 3],
            key=lambda x: x["left"]
        )

        # find trigger index and expand outward while gap <= 20pt
        trigger_idx = next(
            (i for i, el in enumerate(all_line_spans)
             if el["left"] == trigger["left"]), 0)

        collected = [all_line_spans[trigger_idx]]
       # expand right, stay within image bounds
        for el in all_line_spans[trigger_idx + 1:]:
            if (el["left"] <= collected[-1]["left"] + collected[-1].get("width", 50) + 20
                    and el["left"] <= img_right + 10):
                collected.append(el)
            else:
                break
        # expand left, stay within image bounds
        for el in reversed(all_line_spans[:trigger_idx]):
            if (collected[0]["left"] - el["left"] - el.get("width", 50) <= 20
                    and el["left"] >= img_left - 10):
                collected.insert(0, el)
            else:
                break

        collected.sort(key=lambda x: x["left"])
        caption_line = " ".join(
            el["text"].strip() for el in collected
            if el["text"].strip() and not is_credit_line(el["text"])
        )

        # look for a continuation line directly below the trigger,
        # using the same vertical gap as between the image and the trigger
        trigger_bottom = trigger["top"] + min(trigger.get("size", 10), 12)
        img_to_trigger_gap = trigger["top"] - img_bottom
        gap_tolerance = max(3, img_to_trigger_gap * 0.5)
        below_candidates = sorted(
            [el for el in all_text_els
             if el["page"] == trigger["page"]
             and trigger_bottom <= el["top"] <= trigger_bottom + 20
             and img_left <= el["left"] <= img_right
             and el.get("italic")
             and 4 <= len(el["text"].strip()) <= 200
             and not is_footer(el["text"])
             and not is_credit_line(el["text"])],
            key=lambda x: x["top"]
        )
        below_text = below_candidates[0]["text"].strip() if below_candidates else None

        full_caption = caption_line
        if below_text:
            full_caption = caption_line + " " + below_text
        img["caption"] = (above_text + " " + full_caption).strip() if above_text else full_caption

    return images

def count_raw_images(pdf_path):
    """
    Counts every distinct embedded image (by xref) across the ENTIRE PDF,
    with zero filtering, no page-position rules, no quality checks.
    This is the denominator for recall: how many image objects actually
    exist in the file, regardless of what the parser later decides to keep.
    """
    doc = fitz.open(pdf_path)
    xrefs = set()
    for page in doc:
        for img in page.get_images(full=True):
            xrefs.add(img[0])
    doc.close()
    return len(xrefs)

def count_raw_tables(pdf_path):
    """
    Counts every table pdfplumber detects across the ENTIRE PDF, with zero
    filtering, no page-skip rules, no quality checks applied. This is the
    denominator for table recall.
    """
    count = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            count += len(page.find_tables())
    return count

def repair_table_via_docx(pdf_path, page_num, original_table=None):
    import tempfile, os as _os
    try:
        from pdf2docx import Converter
        from docx import Document
    except ImportError:
        return None

    tmp_docx = None
    try:
        tmp_docx = tempfile.NamedTemporaryFile(suffix=".docx", delete=False).name
        cv = Converter(pdf_path)
        cv.convert(tmp_docx, start=page_num - 1, end=page_num)
        cv.close()

        doc = Document(tmp_docx)
        candidates = [t for t in doc.tables if len(t.rows) >= 2 and len(t.columns) >= 2]
        if not candidates:
            return None

        def is_tabular(t):
            lengths = [len(c.text.strip()) for row in t.rows for c in row.cells if c.text.strip()]
            if not lengths:
                return False
            return (sum(lengths) / len(lengths)) <= 60 and max(lengths) <= 200

        tabular_candidates = [t for t in candidates if is_tabular(t)]
        if not tabular_candidates:
            return None

        if original_table:
            orig_rows = len(original_table)
            orig_cols = max((len(r) for r in original_table), default=0)
            best_table = min(
                tabular_candidates,
                key=lambda t: abs(len(t.rows) - orig_rows) + abs(len(t.columns) - orig_cols)
            )
        else:
            best_table = max(tabular_candidates, key=lambda t: len(t.rows) * len(t.columns))

        n_rows = len(best_table.rows)
        n_cols = len(best_table.columns)
        grid = [[None] * n_cols for _ in range(n_rows)]
        for r_idx, row in enumerate(best_table.rows):
            for c_idx, cell in enumerate(row.cells):
                grid[r_idx][c_idx] = cell

        rows_out = [[""] * n_cols for _ in range(n_rows)]
        for r_idx in range(n_rows):
            for c_idx in range(n_cols):
                cell = grid[r_idx][c_idx]
                is_vertical_continuation = (
                    r_idx > 0 and grid[r_idx - 1][c_idx]._tc is cell._tc
                )
                rows_out[r_idx][c_idx] = "" if is_vertical_continuation else cell.text.strip()

        return rows_out
    except Exception:
        return None
    finally:
        if tmp_docx and _os.path.exists(tmp_docx):
            _os.remove(tmp_docx)

def extract_tables(pdf_path):
    result = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            if page_num in SKIP_PAGES:
                continue
            for tbl_obj in page.find_tables():
                table = tbl_obj.extract()
                if not table or len(table) < 3:
                    continue
                if max(len(row) for row in table) < 2:
                    continue
                flat = [c for row in table for c in row if c]
                avg_len = sum(len(c) for c in flat) / max(len(flat), 1)
                if avg_len > 200:
                    continue
                all_cells = [c for row in table for c in row]
                empty_cells = sum(1 for c in all_cells if not c or str(c).strip() == '')
                if empty_cells / max(len(all_cells), 1) > 0.5:
                    continue
               # H38: skip bordered paragraphs masquerading as tables
                if len(table) > 1:
                    single_column_rows = sum(
                        1 for row in table
                        if sum(1 for c in row if c and str(c).strip()) == 1
                    )
                    if single_column_rows / len(table) > 0.60:
                        continue
               # H39: skip tables where column fill is inconsistent across rows
                # real tables have values in the same columns across most rows
                consistent_cols = None
                if len(table) > 2 and max(len(row) for row in table) > 1:
                    col_fill = {}
                    for row in table:
                        for ci, cell in enumerate(row):
                            if cell and str(cell).strip():
                                col_fill[ci] = col_fill.get(ci, 0) + 1
                    if col_fill:
                        max_fill = max(col_fill.values())
                        consistent_cols = sum(1 for v in col_fill.values() 
                                            if v >= max_fill * 0.5)
                        if consistent_cols <= 1:
                            continue

                repaired = repair_table_via_docx(pdf_path, page_num, original_table=table)
                if repaired:
                    orig_nonempty = sum(1 for row in table for c in row if c and str(c).strip())
                    rep_nonempty  = sum(1 for row in repaired for c in row if c and str(c).strip())
                    orig_distinct = len(set(str(c).strip() for row in table for c in row if c and str(c).strip()))
                    rep_distinct  = len(set(str(c).strip() for row in repaired for c in row if c and str(c).strip()))
                    row_ratio = len(repaired) / max(len(table), 1)

                    # Three separate guards, one per confirmed failure mode tonight:
                    # - distinct values collapsing (Legumes "Site" duplication)
                    # - filled cells silently going blank (Normandie missing week)
                    # - row count exploding from fragmentation (BSVn19 shredded rows)
                    reject = (
                        rep_distinct < orig_distinct * 0.7
                        or rep_nonempty < orig_nonempty * 0.85
                        or row_ratio > 2.5
                    )
                    if not reject:
                        table = repaired

                v_rules = sorted(set(
                    round(edge["x0"])
                    for edge in page.edges
                    if edge["orientation"] == "v"
                    and edge["top"] >= tbl_obj.bbox[1] - 2
                    and edge["bottom"] <= tbl_obj.bbox[3] + 2
                ))

                result.append({"page": page_num, "rows": table, "bbox": tbl_obj.bbox, "vertical_rules": v_rules})
    return result

_docling_converter_cache = {}

def _get_docling_converter():
    if 'converter' not in _docling_converter_cache:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions, EasyOcrOptions
        pipeline_options = PdfPipelineOptions()
        pipeline_options.ocr_options = EasyOcrOptions(lang=['fr'])
        pipeline_options.do_table_structure = True
        _docling_converter_cache['converter'] = DocumentConverter(
            format_options={'pdf': PdfFormatOption(pipeline_options=pipeline_options)}
        )
    return _docling_converter_cache['converter']

def docling_extract_tables(pdf_path):
    import fitz

    converter = _get_docling_converter()
    result = converter.convert(pdf_path)
    doc = result.document

    doc_pdf = fitz.open(pdf_path)
    page_heights = {i + 1: page.rect.height for i, page in enumerate(doc_pdf)}
    doc_pdf.close()

    def table_to_html(table):
        cells = table.data.table_cells
        if not cells:
            return None
        n_rows = max(c.end_row_offset_idx for c in cells)
        n_cols = max(c.end_col_offset_idx for c in cells)
        grid = [[None] * n_cols for _ in range(n_rows)]
        for cell in cells:
            grid[cell.start_row_offset_idx][cell.start_col_offset_idx] = cell

        # Reuse the same quality checks as the strict pdfplumber-based
        # extract_tables(), applied to Docling's own cell grid. Confirmed
        # via direct test on the real 30-PDF sample: 21/75 tables dropped,
        # all genuinely sparse or malformed, no false positives observed.
        text_grid = [[(grid[r][c].text if grid[r][c] else None) for c in range(n_cols)] for r in range(n_rows)]

        if len(text_grid) < 3:
            return None
        if n_cols < 2:
            return None

        flat = [c for row in text_grid for c in row if c]
        avg_len = sum(len(c) for c in flat) / max(len(flat), 1)
        if avg_len > 200:
            return None

        all_cells = [c for row in text_grid for c in row]
        empty_cells = sum(1 for c in all_cells if not c or str(c).strip() == '')
        if empty_cells / max(len(all_cells), 1) > 0.5:
            return None

        single_column_rows = sum(
            1 for row in text_grid
            if sum(1 for c in row if c and str(c).strip()) == 1
        )
        if single_column_rows / len(text_grid) > 0.60:
            return None

        col_fill = {}
        for row in text_grid:
            for ci, cell in enumerate(row):
                if cell and str(cell).strip():
                    col_fill[ci] = col_fill.get(ci, 0) + 1
        if col_fill:
            max_fill = max(col_fill.values())
            consistent_cols = sum(1 for v in col_fill.values() if v >= max_fill * 0.5)
            if consistent_cols <= 1:
                return None

        html = []
        covered = set()
        for r in range(n_rows):
            html.append('<tr>')
            for c in range(n_cols):
                if (r, c) in covered:
                    continue
                cell = grid[r][c]
                if cell is None:
                    html.append('<td></td>')
                    continue
                for rr in range(cell.start_row_offset_idx, cell.end_row_offset_idx):
                    for cc in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                        covered.add((rr, cc))
                attrs = ''
                if cell.row_span > 1:
                    attrs += f' rowspan="{cell.row_span}"'
                if cell.col_span > 1:
                    attrs += f' colspan="{cell.col_span}"'
                tag = 'th' if cell.column_header else 'td'
                html.append(f'<{tag}{attrs}>{cell.text}</{tag}>')
            html.append('</tr>')
        return ''.join(html)

    results = []
    for table in doc.tables:
        if not table.prov:
            continue
        prov = table.prov[0]
        page_no = prov.page_no
        page_h = page_heights.get(page_no, 792)

        top = page_h - prov.bbox.t
        bottom = page_h - prov.bbox.b
        bbox = (prov.bbox.l, top, prov.bbox.r, bottom)

        table_html = table_to_html(table)
        if table_html is None:
            continue

        results.append({'page': page_no, 'table_html': table_html, 'bbox': bbox})

    return results

def assign_to_sections(sections, images, tables):
    page_headers = [(p, sec["header_top"], sec["section_index"])
                    for sec in sections for p in sec["pages"]]
    page_headers.sort()

    def find_section(img_page, img_top, img_bottom=None):
        max_distance = 250  # reject if no header is within this distance
        same_level_tolerance = 20

        # prioritize headers that fall inside the image's vertical range
        if img_bottom is not None:
            inside = [(top, idx) for (p, top, idx) in page_headers
                      if p == img_page and img_top < top <= img_bottom]
            if inside:
                return min(inside, key=lambda x: x[0])[1]

        # prioritize headers at roughly the same vertical level (side-by-side layout)
        same_level = [(top, idx) for (p, top, idx) in page_headers
                     if p == img_page and abs(top - img_top) <= same_level_tolerance]
        if same_level:
            return min(same_level, key=lambda x: abs(x[0] - img_top))[1]

        # otherwise, closest header strictly above, on the same page only
        above = [(top, idx) for (p, top, idx) in page_headers
                 if p == img_page and top <= img_top]
        if above:
            closest_top, closest_idx = max(above, key=lambda x: x[0])
            if img_top - closest_top <= max_distance:
                return closest_idx
            return None  # too far, reject the pair

        return None  # no header above on this page at all, reject

    sec_map = {sec["section_index"]: sec for sec in sections}
    for sec in sections:
        sec["images"] = []
        sec["tables"] = []
    for img in images:
        idx = find_section(img["page"], img.get("top", 0), img.get("bottom"))
        if idx is not None and idx in sec_map:
            sec_map[idx]["images"].append(img)
    for t in tables:
        bbox = t.get("bbox")
        tbl_page = t["page"]
        tbl_top  = bbox[1] if bbox else 0

        # find the closest header strictly above the table on the same page
        above = [(top, idx) for (p, top, idx) in page_headers
                 if p == tbl_page and top <= tbl_top]
        if above:
            idx = max(above, key=lambda x: x[0])[1]
            if idx in sec_map:
                sec_map[idx]["tables"].append(t)
                continue

        '''
        # no header above on this page: take the last header from the previous page
        prev = [(top, idx) for (p, top, idx) in page_headers
                if p == tbl_page - 1]
        if prev:
            idx = max(prev, key=lambda x: x[0])[1]
            if idx in sec_map:
                sec_map[idx]["tables"].append(t)
                continue

        # last resort: walk back through earlier pages
        for pg in range(tbl_page - 2, 0, -1):
            earlier = [(top, idx) for (p, top, idx) in page_headers if p == pg]
            if earlier:
                idx = max(earlier, key=lambda x: x[0])[1]
                if idx in sec_map:
                    sec_map[idx]["tables"].append(t)
                break
            '''
    return sections


def parse_pdf(pdf_path, out_img_dir, url=None):
    meta       = extract_metadata(pdf_path, url)
    meta["images_found_total"] = count_raw_images(pdf_path)
    meta["tables_found_total"] = count_raw_tables(pdf_path)
    tables       = extract_tables(pdf_path)
    meta["tables_kept"]    = len(tables)
    meta["tables_dropped"] = meta["tables_found_total"] - meta["tables_kept"]
    table_bboxes = {}
    for t in tables:
        p = t["page"]
        if p not in table_bboxes:
            table_bboxes[p] = []
        table_bboxes[p].append(t["bbox"])
    s_raw, i_raw, text_els = parse_structure(pdf_path, table_bboxes)
    text_els = [
        el for el in text_els
        if not any(
            bx0 <= el["left"] <= bx1 and by0 <= el["top"] <= by1
            for bx0, by0, bx1, by1 in table_bboxes.get(el["page"], [])
        )
    ]
    page_texts = extract_text(pdf_path, table_bboxes)
    sections   = split_into_sections(page_texts, s_raw)

    # Merge empty sections into the next one (handles split header spans)
    merged, i = [], 0
    while i < len(sections):
        sec = sections[i]
        if (len(sec["text"].strip()) == 0
                and i + 1 < len(sections)
                and "STADES" not in sec["section_title"].upper()):
            sections[i + 1]["section_title"] = (
                sec["section_title"] + " " + sections[i + 1]["section_title"]
            )
            i += 1
            continue
        merged.append(sec)
        i += 1
    sections = merged

    # build per-page first header top
    first_header_top = {}
    for sec in s_raw:
        p = sec["page"]
        if p not in first_header_top or sec["top"] < first_header_top[p]:
            first_header_top[p] = sec["top"]

    i_raw = [
    img for img in i_raw
    if (img.get("page_height") is None or img["bottom"] >= 0.10 * img["page_height"])
    and (img["page"] not in first_header_top or img["top"] >= first_header_top[img["page"]])
    ]
    
    images   = extract_images(pdf_path, out_img_dir, meta["bulletin_id"], i_raw, text_els)
    images   = match_captions(images, text_els)
    meta["images_kept"]    = len(images)
    meta["images_dropped"] = meta["images_found_total"] - meta["images_kept"]
    sections = assign_to_sections(sections, images, tables)

    # build local_text for each table
    for sec in sections:
        header_top  = sec.get("header_top", 0)
        header_page = sec["pages"][0] if sec.get("pages") else None
        for tbl in sec.get("tables", []):
            tbl_top  = tbl["bbox"][1] if tbl.get("bbox") else None
            tbl_page = tbl["page"]

            if tbl_top is None:
                tbl["local_text"] = None
                continue

            if abs(tbl_top - header_top) > 20:
                spans = [
                    el for el in text_els
                    if el["page"] == tbl_page
                    and el["top"] > header_top
                    and el["top"] < tbl_top
                    and not is_footer(el["text"])
                ]
            else:
                spans = []

            local_text = " ".join(el["text"].strip() for el in spans if el["text"].strip())
            if is_chart_noise(local_text):
                local_text = ""
            tbl["local_text"] = local_text if len(local_text) > 200 else None

    # build local_text for each image
    for sec in sections:
        header_top = sec.get("header_top", 0)
        for img in sec.get("images", []):
            img_top   = img.get("top")
            img_left  = img.get("left")
            img_right = img.get("right")
            img_page  = img.get("page")

            if img_top is None:
                img["local_text"] = None
                continue

            img_bottom = img.get("bottom", img_top + 50)

            # condition 1: text vertically between header and image
            if abs(img_top - header_top) > 20:
                spans = [
                    el for el in text_els
                    if el["page"] == img_page
                    and el["top"] > header_top
                    and el["top"] < img_top
                    and not is_footer(el["text"])
                ]
            else:
                spans = []

            # condition 2: horizontal fallback (text within image bounds)
            if not spans and img_left is not None:
                spans = [
                    el for el in text_els
                    if el["page"] == img_page
                    and el["left"] >= img_left
                    and el["left"] <= img_right
                    and el["top"] >= header_top
                    and el["top"] < img_bottom
                    and not is_footer(el["text"])
                ]

            # condition 3: left-side collection (side image or header inside image range)
            has_header_inside = any(
                sec.get("pages") and sec["pages"][0] == img_page
                and img_top <= sec.get("header_top", 0) <= img_bottom
                for sec in sections
            )
            collected_so_far = " ".join(el["text"].strip() for el in spans if el["text"].strip())
            if (not spans or len(collected_so_far) <= 200) and (abs(img_top - header_top) <= 20 or has_header_inside) and img_left is not None:
                next_header_top = min(
                    (sec.get("header_top") for sec in sections
                     if sec.get("pages") and sec["pages"][0] == img_page
                     and sec.get("header_top", 0) > header_top),
                    default=float("inf")
                )
                spans = [
                    el for el in text_els
                    if el["page"] == img_page
                    and el["top"] >= header_top
                    and el["top"] <= next_header_top
                    and el["left"] < img_left
                    and not is_footer(el["text"])
                ]

            # condition 4: left-beside collection (text in left column beside the image)
            if img_left is not None:
                beside_left_spans = [
                    el for el in text_els
                    if el["page"] == img_page
                    and el["top"] >= img_top
                    and el["top"] <= img_bottom
                    and el["left"] < img_left
                    and not is_footer(el["text"])
                ]
                spans = spans + beside_left_spans

            # condition 5: right-side collection (image is in right column)
            if img_left is not None and img_left > 150:
                right_spans = [
                    el for el in text_els
                    if el["page"] == img_page
                    and el["top"] >= img_top
                    and el["top"] <= img_bottom
                    and el["left"] >= img_left - 30
                    and not is_footer(el["text"])
                ]
                spans = spans + right_spans

            # deduplicate spans by position to avoid repetition from multiple collection conditions
            seen_positions = set()
            deduped_spans = []
            for el in spans:
                key = (el["page"], round(el["top"]), round(el["left"]))
                if key not in seen_positions:
                    seen_positions.add(key)
                    deduped_spans.append(el)
            spans = deduped_spans


            local_text = " ".join(el["text"].strip() for el in spans if el["text"].strip())
            if is_chart_noise(local_text):
                local_text = ""
            img["local_text"] = local_text if len(local_text) > 200 else None

    return [{**meta, **sec} for sec in sections]

if __name__ == "__main__":
    import os
    import fitz

    pdf_path = (
        Path(__file__).resolve().parents[2]
        / "data" / "local" / "pdfs"
        / "BSV_NA_VIGNE_CHARENTES_02_20190409_cle8ac81e.pdf"
    )
    if not pdf_path.is_file():
        raise FileNotFoundError(
            f"Sample PDF not found at {pdf_path}; hydrate data/local/pdfs first."
        )
    print(os.getcwd())
    print(os.listdir(os.getcwd()))
    doc = fitz.open(pdf_path)
    for page_num, page in enumerate(doc, 1):
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    bold = bool(span["flags"] & 16) or "Bold" in span.get("font", "")
                    if bold and span["size"] <= 13 and len(text) > 3:
                        print(f"p{page_num} size={span['size']:.1f} last={text[-1]!r} text={text!r}")

    text = "Mildiou sur grappe – juin 2020"
    print(is_footer(text))
    print(is_bad_header(text))
    print(is_credit_line(text))


