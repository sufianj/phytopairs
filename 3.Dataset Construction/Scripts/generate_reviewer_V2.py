"""
BSV Pair Reviewer Generator — v3
Adds: editable text field, editable table cells, on top of v2's
validation buttons (GOOD / BORDERLINE / BAD), comment field,
export to JSON, progress counter, and reorganised heuristic panel
(EXTRACTION rules vs PAIRING rules).

Run from inside BSV Sample Construction/:
    python3 generate_reviewer.py
"""

import json, os

PAIRS_FILE = "bsv_pairs_edit_test.jsonl"
IMG_DIR    = "bsv_images_edit_test"
HTML_OUT   = "bsv_reviewer_edit_test.html"

# ---------------------------------------------------------------------------
# Load pairs
# ---------------------------------------------------------------------------

with open(PAIRS_FILE, encoding="utf-8") as f:
    pairs = [json.loads(l) for l in f]

url_lookup = {}
PDF_URLS_PATH = "/projects/agrifutur/yabusaqe/BSV Sample Construction/PDFs Download/PDF_URLS.txt"
if os.path.exists(PDF_URLS_PATH):
    with open(PDF_URLS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("<"):
                line = line[1:]
            url = line.split(">")[0].strip()
            if url.startswith("http"):
                stem = url.rstrip("/").split("/")[-1].replace(".pdf", "")
                url_lookup[stem] = url
for p in pairs:
    if not p.get("url"):
        p["url"] = url_lookup.get(p.get("bulletin_id", ""), "")

# ---------------------------------------------------------------------------
# Compute heuristic values per pair
# ---------------------------------------------------------------------------

def get_image_data(filename):
    path = os.path.join(IMG_DIR, filename)
    if not os.path.exists(path):
        return None, None, None
    try:
        from PIL import Image
        import base64, io
        with Image.open(path) as img:
            true_w, true_h = img.size
            max_dim = 600
            if max(true_w, true_h) > max_dim:
                scale = max_dim / max(true_w, true_h)
                img = img.resize((int(true_w * scale), int(true_h * scale)), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=65)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return true_w, true_h, f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None, None, None

def table_stats(rows):
    if not rows:
        return 0, 0, 0.0
    n_rows = len(rows)
    n_cols = max(len(r) for r in rows)
    all_cells = [c for r in rows for c in r]
    empty = sum(1 for c in all_cells if not c or str(c).strip() == "")
    empty_pct = empty / max(len(all_cells), 1) * 100
    return n_rows, n_cols, empty_pct

enriched = []
for p in pairs:
    e = dict(p)
    if p.get("image"):
        fname = p["image"].get("filename", "")
        w, h, b64 = get_image_data(fname)
        e["img_width"]  = w
        e["img_height"] = h
        e["img_area"]   = (w * h) if (w and h) else None
        e["img_ratio"]  = round(w / h, 2) if (w and h and h > 0) else None
        e["img_page"]   = p["image"].get("page")
        e["img_b64"]    = b64
    if p.get("table"):
        rows = p["table"].get("rows", [])
        nr, nc, ep = table_stats(rows)
        e["tbl_rows"]      = nr
        e["tbl_cols"]      = nc
        e["tbl_empty_pct"] = round(ep, 1)
    enriched.append(e)

pairs_json = json.dumps(enriched, ensure_ascii=False)

# ---------------------------------------------------------------------------
# All 32 heuristics reference table
# (TYPE column: EXTRACTION = why element was kept, PAIRING = why elements were paired)
# ---------------------------------------------------------------------------

HEURISTICS = [
    ("H01", "Image", "EXTRACTION",
     "Skip images whose raw file size is below 5,000 bytes. "
     "Images this small are almost always 1×1 tracking pixels, tiny bullet icons, or corrupt embedded objects. "
     "Applied before reading pixel dimensions to avoid wasting time decoding useless data.",
     "filesize &lt; 5,000 bytes"),

    ("H02", "Image", "EXTRACTION",
     "Skip images whose pixel area (width × height) is below 200,000 px². "
     "Institutional logos, government stamps, and risk meter icons are typically small. "
     "Real agricultural content such as field photos, charts, and observation maps are consistently larger. "
     "The threshold of 200,000 px² was chosen after visually inspecting 50 extracted images across 4 parser iterations.",
     "width × height &lt; 200,000 px²"),

    ("H03", "Image", "EXTRACTION",
     "Skip images whose width to height ratio exceeds 8. "
     "BSV bulletins use thin horizontal bars as visual separators between sections, and some have wide institutional footer strips. "
     "These are extremely wide and short, giving ratios above 8. "
     "Real content images such as photos, charts, and maps are never this wide relative to their height.",
     "width / height &gt; 8"),

    ("H04", "Image", "EXTRACTION",
     "Skip all images found on page 1 when the bulletin has more than 2 pages. "
     "Page 1 of a multi-page BSV bulletin is always a cover page containing the bulletin banner, the Écophyto logo, institutional headers, and decorative crop photos embedded in the title design. "
     "None of these are paired with article content. "
     "For 2-page bulletins the rule does not apply because page 1 contains real content.",
     "page = 1 AND total_pages &gt; 2"),

    ("H05", "Image", "EXTRACTION",
     "Skip images whose internal PDF xref ID appears on more than 2 pages. "
     "In PDF internals a recurring image such as a page header logo is stored once and referenced by the same xref on every page it appears. "
     "By counting how many distinct pages reference each xref, we identify and discard these repeating decorative elements. "
     "Applied during parse_structure() at runtime; the xref counts are not stored in the output JSONL.",
     "xref_page_count &gt; 2"),

    ("H06", "Section", "EXTRACTION",
     "A text span must be marked as bold to be considered a section header candidate. "
     "BSV section titles are always rendered in bold. "
     "Boldness is detected from the pymupdf font flag (bit 4) or by checking whether the string 'Bold' appears in the font family name.",
     "font flag bit 4 = True OR 'Bold' in font name"),

    ("H07", "Section", "EXTRACTION",
     "A bold span must have a font size at or above 85% of the effective maximum bold font size in the document. "
     "This dynamic threshold adapts to each bulletin's own typography instead of relying on a fixed point size. "
     "It ensures that body text rendered in bold (such as inline emphasis or table headers) is not mistaken for a section title.",
     "span size ≥ 0.85 × effective_max_bold_size"),

    ("H08", "Section", "EXTRACTION",
     "When computing the effective maximum bold font size, sizes that appear 2 times or fewer AND are more than 1.3 times larger than the next size down are excluded as decorative outliers. "
     "Some BSV bulletins have a single very large decorative title on the cover page that would otherwise inflate the threshold and suppress all real section headers. "
     "Excluding these rare outsized sizes keeps the threshold grounded in the document's recurring heading fonts.",
     "count ≤ 2 AND size &gt; 1.3 × next_size → excluded"),

    ("H09", "Section", "EXTRACTION",
     "A bold span must begin at least 15 pixels from the left edge of the page. "
     "Some BSV bulletins have sidebar text running vertically along the left margin. "
     "This text is sometimes bold and large but is not a section title. "
     "The 15 pixel margin filters it out without affecting normally positioned headings.",
     "x0 ≥ 15 px"),

    ("H10", "Section", "EXTRACTION",
     "A section title candidate must be shorter than 80 characters. "
     "Genuine section headers such as 'MILDIOU', 'Situation actuelle', or 'Bilan météo' are always concise. "
     "Longer bold text is usually a sentence, a legal notice, or a block of emphasis text embedded in a paragraph.",
     "len(title) &lt; 80 characters"),

    ("H11", "Section", "EXTRACTION",
     "A section title candidate must be at least 3 characters long. "
     "Single or double character bold spans appear in BSV layouts as decorative initials, bullet characters rendered in large bold font, or the first letter of page marker text such as 'P' from '<<<PAGE_2>>>'. "
     "These caused page markers to leak into section text before this filter was added.",
     "len(title) ≥ 3 characters"),

    ("H12", "Section", "EXTRACTION",
     "A section title candidate must not match any pattern in the is_bad_header() function. "
     "This function checks for known non-content patterns including single lowercase words, known sidebar institution names, pure number strings, and bulletin metadata strings. "
     "The list was built incrementally as specific false positives appeared during parser development.",
     "not in is_bad_header() pattern list"),

    ("H13", "Section", "EXTRACTION",
     "When two bold spans appear on the same page within 8 pixels of each other vertically, they are merged into a single section title. "
     "Some BSV section headers are split across two text spans in the PDF structure, for example 'Point' and 'météo/Phénologie' appearing as two consecutive bold blocks 3 pixels apart. "
     "Without merging, these would produce two separate sections instead of one.",
     "same page AND vertical gap ≤ 8 px → merge"),

    ("H14", "Table", "EXTRACTION",
     "Skip tables with fewer than 3 rows. "
     "pdfplumber detects tables by finding rectangular borders in the PDF. "
     "Very small grids of 1 or 2 rows are almost always decorative boxes, risk evaluation color legends, or layout artifacts rather than real observation data tables.",
     "row count &lt; 3 → skip"),

    ("H15", "Table", "EXTRACTION",
     "Skip tables with fewer than 2 columns. "
     "A single-column table is almost never a real data table. "
     "It usually means pdfplumber found a bordered text box and misread its interior text as a single-cell table.",
     "column count &lt; 2 → skip"),

    ("H16", "Table", "EXTRACTION",
     "Skip tables where the average non-empty cell text length exceeds 200 characters. "
     "When pdfplumber detects a bordered paragraph or a large risk description box as a table, all the text ends up in one or two very long cells. "
     "Real observation data tables have short cell values such as dates, numbers, percentages, and place names.",
     "avg non-empty cell length &gt; 200 chars → skip"),

    ("H17", "Table", "EXTRACTION",
     "Skip tables where more than 50% of cells are empty or None. "
     "BSV bulletins use heavily merged cells for multi-row and multi-column headers. "
     "pdfplumber fills merged spans with the top-left cell value and leaves the rest as None. "
     "Tables where most cells are None are either fully empty layout grids or so fragmented by merging that their structure is unreadable. "
     "The 50% threshold keeps real sparse tables such as weekly weather forecasts while removing empty artifacts.",
     "empty cells / total cells &gt; 50% → skip"),

    ("H18", "Text", "EXTRACTION",
     "When searching for a section title's position in the full PDF text string, the search starts from the position of the page marker <<<PAGE_N>>> where N is the page the header was detected on. "
     "Without this, the search could match the same title word earlier in the document, for example in a table of contents on the cover page, and then extract the wrong body text for that section.",
     "search from <<<PAGE_N>>> position, not from document start"),

    ("H19", "Text", "EXTRACTION",
     "Bullet prefix characters are stripped from a section title before searching for it in the full text string. "
     "Some BSV section headers begin with a bullet character (•, the Unicode private use character \\uf0b7, or a plain hyphen) that is rendered as part of the title in the PDF but appears differently in the extracted text. "
     "Stripping these ensures the title match succeeds.",
     "strip •, \\uf0b7, and leading hyphens before matching"),

    ("H20", "Text", "EXTRACTION",
     "For PDFs where no section structure was detected, the entire PDF text is returned as a single 'Document' fallback record. "
     "Without this fix, the page transition markers <<<PAGE_1>>>, <<<PAGE_2>>> etc. that we insert to track page boundaries appear verbatim in the text field of the output record. "
     "This rule strips them with a regex before writing the fallback text to the JSONL.",
     "re.sub(r'<<<PAGE_\\d+>>>', '', full_text)"),

    ("H21", "Assign", "PAIRING",
     "Each extracted image is assigned to the section whose header appears closest above it on the same page, measured by vertical position in PDF coordinates. "
     "This reflects the standard BSV layout convention: a photo or chart always appears below the heading of the article it illustrates. "
     "The vertical distance between the image's top edge and each candidate header's top edge is computed; the smallest positive distance wins.",
     "argmin(header_top − image_top) where header_top &lt; image_top, same page"),

    ("H22", "Assign", "PAIRING",
     "If no section header appears above an image on its page (the image is at the very top before any heading), the image is assigned to the first header on that page. "
     "This handles cases where a full-page illustration appears before the section title in the PDF's reading order, which occasionally happens in bulletin annexes.",
     "fallback: first header on the page"),

    ("H23", "Section", "EXTRACTION",
     "If the text body of a detected section is completely empty after splitting, that section's title is concatenated onto the title of the immediately following section. "
     "This handles PDFs where two consecutive bold lines together form one logical header but were detected as two separate candidates. "
     "Without this merge, one section would have a title but no content and the following section would have incomplete title information.",
     "if section text == '' → prepend title to next section"),

    ("H24", "Pair", "PAIRING",
     "Sections where the title is 'Document' are excluded from pair generation entirely. "
     "The 'Document' title is assigned as a fallback when our parser finds no bold headers in a PDF and cannot segment it into sections. "
     "In these cases the entire PDF becomes one undifferentiated block, and any image or table within it has no meaningful structural relationship to the text.",
     "section_title == 'Document' → skip all pairs from this section"),

    ("H25", "Pair", "PAIRING",
     "Sections whose text body is shorter than 50 characters are excluded from pair generation. "
     "Very short text such as 'Quelques pontes' (15 characters) or a single risk label like 'Le risque est élevé' cannot form a meaningful text pairing because there is not enough semantic content to describe an agricultural observation. "
     "The 50-character threshold was chosen to allow short but complete sentences while excluding stub fragments.",
     "len(text.strip()) &lt; 50 → skip all pairs from this section"),

    ("H26", "Pair", "PAIRING",
     "A section must contain at least two distinct modalities (text, image, or table) to generate any pairs. "
     "A section with only text and no images or tables cannot produce a cross-modal pair. "
     "In the final clean run on 1,000 PDFs, 5,046 out of 6,992 sections were single-modality and were skipped by this rule.",
     "count({has_text, has_image, has_table}) ≥ 2"),

    ("H27", "Pair", "PAIRING",
     "For sections with both text and at least one image, one text+image pair is generated per image. "
     "If a section contains three images, three separate text+image pairs are generated, each sharing the same section text but pointing to a different image. "
     "This ensures every image in a section gets a paired text record.",
     "1 pair per image in section"),

    ("H28", "Pair", "PAIRING",
     "For sections with both text and at least one table, one text+table pair is generated per table. "
     "If a section contains two tables, two separate text+table pairs are generated. "
     "Same logic as H27 but for tables.",
     "1 pair per table in section"),

    ("H29", "Pair", "PAIRING",
     "For sections that contain both images and tables (regardless of whether text is present), pairs are generated for every combination of image and table in the section. "
     "A section with 2 images and 2 tables produces 4 image+table pairs. "
     "This cross-product approach ensures no image-table co-occurrence within a section is missed.",
     "1 pair per (image, table) combination"),

    ("H30", "Meta", "EXTRACTION",
     "The bulletin date is extracted from the PDF filename by trying four regex patterns in order: 8-digit YYYYMMDD starting with '20', 8-digit DDMMYYYY where the last 4 digits form a valid year, YYYY-MM-DD or YYYY_MM_DD with separators, and DD-MM-YY or DD_MM_YY with 2-digit year. "
     "The ordered priority ensures that the most unambiguous format is tried first. "
     "Without ordered priority the patterns could conflict: a filename like '20190619' could be misread as DD=20, MM=19 (invalid month) if DDMMYYYY were tried first.",
     "YYYYMMDD → DDMMYYYY → YYYY-MM-DD → DD-MM-YY"),

    ("H31", "Meta", "EXTRACTION",
     "After a date is parsed by H30, it is validated before being stored. "
     "The day must be between 1 and 31, the month between 1 and 12, and the year between 2000 and 2030. "
     "This catches malformed filenames that accidentally match a date pattern but produce impossible values such as month 20 or year 1409.",
     "1 ≤ day ≤ 31 AND 1 ≤ month ≤ 12 AND 2000 ≤ year ≤ 2030"),

    ("H32", "Meta", "EXTRACTION",
     "The crop category is detected by scanning the PDF filename for keyword substrings from a predefined dictionary called CROP_CATEGORIES. "
     "BSV filenames systematically encode the crop type as a keyword such as 'viti', 'maraichage', 'grandes_cultures', 'arbo', 'lin', 'colza', and so on. "
     "The dictionary maps each keyword to a human-readable category name. "
     "Note: per supervisor feedback, this metadata will be replaced in a future version by a SPARQL query to the PHB Knowledge Graph where it is already stored.",
     "keyword match in filename against CROP_CATEGORIES dict"),
    ("H33", "Section", "EXTRACTION",
 "Text color brightness above 200 forces level 1 classification. "
 "Main section headers in BSV bulletins are often rendered as white or light-colored text on a colored background box. "
 "Brightness is computed as 0.299×R + 0.587×G + 0.114×B from the RGB color value.",
 "brightness(color) > 200 → level 1"),

    ("H34", "Section", "EXTRACTION",
     "Skip bold spans whose text ends with punctuation marks. "
     "Genuine section headers never end with a period, comma, semicolon, exclamation mark, or question mark. "
     "This filter removes bold emphasis text embedded in body paragraphs such as 'Note importante.' or 'Attention,'.",
     "last char not in . ! ? , ;"),

    ("H35", "Section", "EXTRACTION",
     "Skip bold spans whose text is entirely lowercase. "
     "Section titles always contain at least one uppercase letter. "
     "Fully lowercase bold spans are almost always hyperlink fragments split across PDF text spans, such as 'mildiou' or 'black rot' appearing inside 'Consultez la fiche «mildiou»'.",
     "text != text.lower()"),
    ("H36", "Image", "EXTRACTION",
 "Skip images where more than 80% of pixels are near-white (R>240, G>240, B>240). "
 "Institutional header banners, blank letterhead pages, and footer contact blocks "
 "have large white backgrounds that real agricultural content images do not.",
 "near_white / total > 0.80"),

    ("H37", "Image", "EXTRACTION",
 "Skip images where a single quantized color dominates more than 70% of pixels. "
 "Solid-color decorative blocks (orange contact pages, colored banners) fail this check "
 "while photographs with natural color variation always pass.",
 "max(color_counts.values()) / total > 0.70"),
]

heuristics_rows = ""
for hid, cat, htype, desc, threshold in HEURISTICS:
    type_cls = "htype-extraction" if htype == "EXTRACTION" else "htype-pairing"
    heuristics_rows += f"""
    <tr>
      <td class="hid" style="vertical-align:top;white-space:nowrap">{hid}</td>
      <td class="hcat hcat-{cat.lower()}" style="vertical-align:top;white-space:nowrap">{cat}</td>
      <td class="htype {type_cls}" style="vertical-align:top;white-space:nowrap">{htype}</td>
      <td style="vertical-align:top;line-height:1.6;font-size:11px">{desc}</td>
      <td class="thresh" style="vertical-align:top;white-space:nowrap">{threshold}</td>
    </tr>"""

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>BSV Pair Reviewer</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  font-family: Arial, sans-serif;
  font-size: 13px;
  background: #fff;
  color: #000;
  height: 100vh;
  display: flex;
  flex-direction: column;
}}
table, th, td {{
  border: 1px solid;
}}
/* ── Top bar ── */
#topbar {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 2px solid #000;
  background: #000;
  color: #fff;
  flex-shrink: 0;
  flex-wrap: wrap;
}}
#topbar h1 {{ font-size: 13px; font-weight: bold; letter-spacing: 1px; text-transform: uppercase; }}
#counter   {{ font-size: 13px; }}
#progress  {{ font-size: 12px; color: #aaa; margin-left: auto; }}
.nav-btn {{
  background: #fff; color: #000; border: none;
  padding: 4px 12px; font-family: Arial, sans-serif;
  font-size: 12px; font-weight: bold; cursor: pointer;
}}
.nav-btn:hover {{ background: #ddd; }}
#ref-btn {{
  background: transparent; color: #fff; border: 1px solid #fff;
  padding: 4px 10px; font-family: Arial, sans-serif;
  font-size: 11px; cursor: pointer;
}}
#ref-btn:hover {{ background: #333; }}
#export-btn {{
  background: #fff; color: #000; border: none;
  padding: 4px 10px; font-family: Arial, sans-serif;
  font-size: 11px; font-weight: bold; cursor: pointer;
}}
#export-btn:hover {{ background: #ddd; }}
#shortcuts {{ font-size: 10px; color: #aaa; }}

/* ── Main layout ── */
#main {{ display: flex; flex: 1; overflow: hidden; }}

/* ── Content panel ── */
#content-panel {{
  flex: 6; padding: 16px; overflow-y: auto;
  border-right: 1px solid #ccc;
}}
.pair-type-badge {{
  display: inline-block; font-size: 10px; font-weight: bold;
  letter-spacing: 1px; text-transform: uppercase;
  border: 1px solid #000; padding: 2px 8px; margin-bottom: 12px;
}}
.section-label {{
  font-size: 15px; font-weight: bold; margin-bottom: 12px;
  border-left: 3px solid #000; padding-left: 8px;
}}
.modality-label {{
  font-size: 10px; font-weight: bold; letter-spacing: 1px;
  text-transform: uppercase; color: #666; margin: 14px 0 4px 0;
}}
#text-box {{
  width: 100%;
  min-height: 120px;
  background: #f5f5f5; border: 1px solid #ccc;
  padding: 10px 12px; font-size: 12px; line-height: 1.6;
  max-height: 200px; overflow-y: auto; white-space: pre-wrap;
  font-family: Arial, sans-serif;
  resize: vertical;
}}
.edit-hint {{ font-size: 9px; color: #888; margin-top: 3px; font-style: italic; }}
#image-box {{ margin: 8px 0; }}
#image-box img {{
  max-width: 100%; max-height: 320px; object-fit: contain;
  border: 1px solid #ccc; display: block;
}}
#caption-box {{ font-size: 11px; color: #444; font-style: italic; margin-top: 4px; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 11px; margin-top: 4px; }}
.data-table th {{ background: #000; color: #fff; padding: 0; text-align: left; font-weight: bold; border: 1px solid #333; }}
.data-table td {{ padding: 0; border: 1px solid #333; }}
.data-table input {{
  width: 100%; border: none; padding: 4px 8px; font-size: 11px;
  font-family: Arial, sans-serif; background: transparent;
}}
.data-table th input {{ color: #fff; font-weight: bold; }}
.data-table input:focus {{ background: #ffffcc; outline: 1px solid #666; }}
.data-table tr:nth-child(even) td {{ background: #f9f9f9; }}
.table-wrap {{ max-height: 260px; overflow-y: auto; border: 1px solid #ccc; }}
.more-rows {{ color: #888; font-style: italic; font-size: 10px; padding: 4px 8px; }}

/* ── Info panel ── */
#info-panel {{
  flex: 4; padding: 16px; overflow-y: auto; background: #fafafa;
}}
.info-section {{ margin-bottom: 16px; }}
.info-title {{
  font-size: 10px; font-weight: bold; letter-spacing: 1px;
  text-transform: uppercase; border-bottom: 1px solid #000;
  padding-bottom: 3px; margin-bottom: 8px;
}}
.info-row {{ display: flex; gap: 8px; margin-bottom: 4px; font-size: 12px; }}
.info-key {{ color: #666; min-width: 90px; flex-shrink: 0; }}
.info-val {{ font-weight: bold; word-break: break-all; }}
.info-val.null {{ color: #aaa; font-weight: normal; font-style: italic; }}

.panel-group-label {{
  font-size: 9px; font-weight: bold; letter-spacing: 1.5px;
  text-transform: uppercase; color: #fff; background: #000;
  padding: 2px 6px; margin-bottom: 8px; display: inline-block;
}}
.panel-group-label.pairing {{ background: #555; }}

.heuristic-row {{ display: flex; gap: 8px; margin-bottom: 5px; font-size: 11px; align-items: flex-start; }}
.h-label {{ color: #555; min-width: 110px; flex-shrink: 0; }}
.h-value {{ font-family: monospace; font-size: 11px; }}
.h-pass  {{ color: #2a7a2a; font-weight: bold; }}
.h-fail  {{ color: #c00; font-weight: bold; }}
.h-na    {{ color: #aaa; }}

/* ── Validation section ── */
#validation-section {{
  margin-bottom: 16px; padding: 12px;
  border: 2px solid #000; background: #fff;
}}
.val-title {{
  font-size: 10px; font-weight: bold; letter-spacing: 1px;
  text-transform: uppercase; margin-bottom: 8px;
}}
.val-buttons {{ display: flex; gap: 8px; margin-bottom: 10px; }}
.val-btn {{
  flex: 1; padding: 8px 4px; font-family: Arial, sans-serif;
  font-size: 11px; font-weight: bold; cursor: pointer;
  border: 2px solid #000; background: #fff; color: #000;
  text-align: center; letter-spacing: 0.5px;
}}
.val-btn:hover {{ background: #f0f0f0; }}
.val-btn.active-good {{ background: #000; color: #fff; }}
.val-btn.active-borderline {{ background: #555; color: #fff; }}
.val-btn.active-bad {{ background: #000; color: #fff; border-style: dashed; }}
.val-shortcut {{ font-size: 9px; font-weight: normal; display: block; margin-top: 2px; color: inherit; opacity: 0.7; }}
#val-comment {{
  width: 100%; height: 56px; resize: vertical;
  font-family: Arial, sans-serif; font-size: 11px;
  border: 1px solid #ccc; padding: 6px; margin-top: 4px;
}}
.val-comment-label {{ font-size: 10px; color: #666; margin-bottom: 3px; }}

/* ── Heuristics reference modal ── */
#ref-overlay {{
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.6); z-index: 100;
  overflow-y: auto; padding: 32px;
}}
#ref-overlay.visible {{ display: block; }}
#ref-box {{
  background: #fff; border: 2px solid #000;
  max-width: 960px; margin: 0 auto; padding: 24px;
}}
#ref-box h2 {{
  font-size: 14px; text-transform: uppercase; letter-spacing: 1px;
  margin-bottom: 16px; border-bottom: 2px solid #000; padding-bottom: 6px;
}}
.ref-table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
.ref-table th {{
  background: #000; color: #fff; padding: 5px 8px;
  text-align: left; font-size: 10px; letter-spacing: 0.5px; text-transform: uppercase;
}}
.ref-table td {{ padding: 5px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
.hid {{ font-family: monospace; font-weight: bold; white-space: nowrap; }}
.thresh {{ font-family: monospace; font-size: 10px; color: #333; }}
.hcat {{ font-size: 10px; font-weight: bold; white-space: nowrap; padding: 2px 6px; }}
.hcat-image   {{ background: #e8e8e8; }}
.hcat-section {{ background: #ddd; }}
.hcat-table   {{ background: #ebebeb; }}
.hcat-text    {{ background: #e4e4e4; }}
.hcat-assign  {{ background: #e0e0e0; }}
.hcat-pair    {{ background: #d8d8d8; }}
.hcat-meta    {{ background: #cfcfcf; }}
.htype {{ font-size: 9px; font-weight: bold; letter-spacing: 0.5px; padding: 2px 5px; white-space: nowrap; }}
.htype-extraction {{ background: #000; color: #fff; }}
.htype-pairing    {{ background: #555; color: #fff; }}
#close-ref {{
  margin-top: 16px; background: #000; color: #fff;
  border: none; padding: 6px 16px;
  font-family: Arial, sans-serif; font-size: 12px; cursor: pointer;
}}
</style>
</head>
<body>

<div id="topbar">
  <h1>BSV Pair Reviewer</h1>
  <button class="nav-btn" onclick="go(-1)">&#8592; Prev</button>
  <button class="nav-btn" onclick="go(1)">Next &#8594;</button>
  <span id="counter">1 / {len(pairs)}</span>
  <span id="progress">Validated: 0 / {len(pairs)}</span>
  <button id="ref-btn" onclick="toggleRef()">&#9776; Heuristics Reference</button>
  <button id="export-btn" onclick="exportValidations()">&#8595; Export JSON</button>
  <button id="export-gt-btn" onclick="exportFinalGT()" style="background:#fff;color:#000;border:none;padding:4px 10px;font-family:Arial,sans-serif;font-size:11px;font-weight:bold;cursor:pointer">&#8595; Export Final GT</button>
  <button id="save-btn" onclick="saveHTML()" style="background:#fff;color:#000;border:none;padding:4px 10px;font-family:Arial,sans-serif;font-size:11px;font-weight:bold;cursor:pointer">&#8595; Save HTML</button>
  <span id="shortcuts">&#8592; &#8594; navigate &nbsp;|&nbsp; 1 GOOD &nbsp; 2 BORDERLINE &nbsp; 3 BAD</span>
</div>

<div id="main">
  <div id="content-panel">
    <div class="pair-type-badge" id="badge-type"></div>
    <div class="section-label" id="section-title"></div>

    <div id="text-section">
      <div class="modality-label">Text (editable)</div>
      <textarea id="text-box" oninput="saveTextEdit()"></textarea>
      <div class="edit-hint">Edits are saved automatically and included in Export JSON / Save HTML.</div>
    </div>

    <div id="image-section">
      <div class="modality-label">Image</div>
      <div id="image-box"></div>
      <div id="caption-box"></div>
    </div>

    <div id="table-section">
      <div class="modality-label">Table (editable)</div>
      <div class="table-wrap"><textarea id="table-box" style="width:100%;min-height:200px;font-family:monospace;font-size:11px;line-height:1.5;padding:10px 12px;border:1px solid #ccc;background:#f5f5f5" oninput="saveTableEdit()"></textarea></div>
      <div class="edit-hint">Edit the raw HTML directly, use &lt;td rowspan="2"&gt; or &lt;td colspan="2"&gt; for merged cells. Rendered preview below.</div>
      <div id="table-preview" style="margin-top:8px"></div>
      <div class="edit-hint">Click any cell to edit. Edits are saved automatically.</div>
    </div>
  </div>

  <div id="info-panel">

    <!-- VALIDATION -->
    <div id="validation-section">
      <div class="val-title">Manual Validation</div>
      <div class="val-buttons">
        <button class="val-btn" id="btn-good"       onclick="setVal('GOOD')">
          ✓ GOOD<span class="val-shortcut">key: 1</span>
        </button>
        <button class="val-btn" id="btn-borderline" onclick="setVal('BORDERLINE')">
          ? BORDERLINE<span class="val-shortcut">key: 2</span>
        </button>
        <button class="val-btn" id="btn-bad"        onclick="setVal('BAD')">
          ✗ BAD<span class="val-shortcut">key: 3</span>
        </button>
      </div>
      <div class="val-comment-label">Comment (optional):</div>
      <textarea id="val-comment" placeholder="e.g. ceratite text paired with pear variety table — wrong page assignment"
        oninput="saveComment()"></textarea>
    </div>

    <!-- METADATA -->
    <div class="info-section">
      <div class="info-title">Metadata</div>
      <div class="info-row"><span class="info-key">Bulletin</span><span class="info-val" id="m-bulletin"></span></div>
      <div class="info-row"><span class="info-key">Crop</span><span class="info-val" id="m-crop"></span></div>
      <div class="info-row"><span class="info-key">Date</span><span class="info-val" id="m-date"></span></div>
      <div class="info-row"><span class="info-key">Year</span><span class="info-val" id="m-year"></span></div>
      <div class="info-row"><span class="info-key">Region</span><span class="info-val" id="m-region"></span></div>
    </div>

    <!-- PAIRING HEURISTICS -->
    <div class="info-section">
      <div class="info-title">Heuristics</div>
      <div class="panel-group-label pairing">PAIRING — why these elements were paired</div>
      <div class="heuristic-row">
        <span class="h-label">Rule (H26–H29)</span>
        <span class="h-value">Same detected section</span>
      </div>
      <div class="heuristic-row">
        <span class="h-label">Section (H24)</span>
        <span class="h-value" id="h-section"></span>
      </div>
      <div class="heuristic-row">
        <span class="h-label">Title length (H11)</span>
        <span class="h-value" id="h-title-len"></span>
      </div>
      <div class="heuristic-row">
          <span class="h-label">Section level</span>
          <span class="h-value" id="h-section-level"></span>
        </div>
    </div>

    <!-- EXTRACTION HEURISTICS — IMAGE -->
    <div class="info-section" id="img-heuristics" style="display:none">
      <div class="panel-group-label">EXTRACTION — why this image was kept</div>
      <div class="heuristic-row"><span class="h-label">Filename</span><span class="h-value" id="h-img-fname"></span></div>
      <div class="heuristic-row"><span class="h-label">Page (H04)</span><span class="h-value" id="h-img-page"></span></div>
      <div class="heuristic-row"><span class="h-label">Dimensions</span><span class="h-value" id="h-img-dims"></span></div>
      <div class="heuristic-row"><span class="h-label">Area (H02)</span><span class="h-value" id="h-img-area"></span></div>
      <div class="heuristic-row"><span class="h-label">Aspect ratio (H03)</span><span class="h-value" id="h-img-ratio"></span></div>
      <div class="heuristic-row"><span class="h-label">Caption</span><span class="h-value" id="h-img-caption"></span></div>
    </div>

    <!-- EXTRACTION HEURISTICS — TABLE -->
    <div class="info-section" id="tbl-heuristics" style="display:none">
      <div class="panel-group-label">EXTRACTION — why this table was kept</div>
      <div class="heuristic-row"><span class="h-label">Page</span><span class="h-value" id="h-tbl-page"></span></div>
      <div class="heuristic-row"><span class="h-label">Rows (H14)</span><span class="h-value" id="h-tbl-rows"></span></div>
      <div class="heuristic-row"><span class="h-label">Columns (H15)</span><span class="h-value" id="h-tbl-cols"></span></div>
      <div class="heuristic-row"><span class="h-label">Empty cells (H17)</span><span class="h-value" id="h-tbl-empty"></span></div>
      <div class="heuristic-row"><span class="h-label">Vertical rules</span><span class="h-value" id="h-tbl-vrules"></span></div>
    </div>

    <!-- EXTRACTION HEURISTICS — TEXT -->
    <div class="info-section" id="text-heuristics" style="display:none">
      <div class="panel-group-label">EXTRACTION — why this text was kept</div>
      <div class="heuristic-row"><span class="h-label">Length (H25)</span><span class="h-value" id="h-txt-len"></span></div>
    </div>

  </div>
</div>

<!-- Export modal -->
<div id="export-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:200;overflow-y:auto;padding:32px">
  <div style="background:#fff;border:2px solid #000;max-width:700px;margin:0 auto;padding:24px">
    <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;border-bottom:2px solid #000;padding-bottom:6px">
      Export Validations — Copy and save as bsv_validations.json on OcciDATA
    </h2>
    <p style="font-size:11px;color:#555;margin-bottom:8px">Select all (Ctrl+A), copy (Ctrl+C), then paste into a new file on OcciDATA.</p>
    <textarea id="export-text" style="width:100%;height:400px;font-family:monospace;font-size:11px;border:1px solid #ccc;padding:8px;resize:vertical" readonly></textarea>
    <button onclick="document.getElementById('export-overlay').style.display='none'"
      style="margin-top:12px;background:#000;color:#fff;border:none;padding:6px 16px;font-family:Arial,sans-serif;font-size:12px;cursor:pointer">
      Close
    </button>
  </div>
</div>

<!-- Heuristics reference modal -->
<div id="ref-overlay">
  <div id="ref-box">
    <h2>All 32 Heuristics — EXTRACTION (why element was kept) vs PAIRING (why elements were paired)</h2>
    <table class="ref-table">
      <thead>
        <tr><th>ID</th><th>Category</th><th>Type</th><th>Rule</th><th>Threshold</th></tr>
      </thead>
      <tbody>{heuristics_rows}</tbody>
    </table>
    <button id="close-ref" onclick="toggleRef()">Close</button>
  </div>
</div>

<script>
const pairs = {pairs_json};
let cur = 0;
const validations = {{}};  // {{ index: {{ label, comment, edited_text, edited_table }} }}

function updateProgress() {{
  const n = Object.keys(validations).length;
  document.getElementById('progress').textContent = `Validated: ${{n}} / ${{pairs.length}}`;
}}

function setVal(label) {{
  if (!validations[cur]) validations[cur] = {{ label: '', comment: '' }};
  validations[cur].label = label;
  updateProgress();
  renderValButtons();
}}

function saveComment() {{
  if (!validations[cur]) validations[cur] = {{ label: '', comment: '' }};
  validations[cur].comment = document.getElementById('val-comment').value;
}}

function saveTextEdit() {{
  if (!validations[cur]) validations[cur] = {{ label: '', comment: '' }};
  validations[cur].edited_text = document.getElementById('text-box').value;
}}

function rowsToHtml(rows) {{
  // Converts the old flat-grid format to a starting HTML representation.
  // Repeated horizontal values collapse into colspan; this is a best-effort
  // starting point, the validator confirms or corrects it by hand.
  let html = '';
  for (let i = 0; i < rows.length; i++) {{
    html += '<tr>';
    const row = rows[i] || [];
    let j = 0;
    while (j < row.length) {{
      let span = 1;
      while (j + span < row.length && row[j + span] === row[j] && row[j] !== '' && row[j] != null) span++;
      const tag = i === 0 ? 'th' : 'td';
      const spanAttr = span > 1 ? ` colspan="${{span}}"` : '';
      html += `<${{tag}}${{spanAttr}}>${{row[j] ?? ''}}</${{tag}}>`;
      j += span;
    }}
    html += '</tr>';
  }}
  return html;
}}

function saveTableEdit() {{
  if (!validations[cur]) validations[cur] = {{ label: '', comment: '' }};
  const html = document.getElementById('table-box').value;
  validations[cur].edited_table_html = html;
  document.getElementById('table-preview').innerHTML = '<table class="data-table"><tbody>' + html + '</tbody></table>';
}}

function renderValButtons() {{
  const v = validations[cur];
  const label = v ? v.label : '';
  document.getElementById('btn-good').className       = 'val-btn' + (label === 'GOOD'       ? ' active-good'       : '');
  document.getElementById('btn-borderline').className = 'val-btn' + (label === 'BORDERLINE' ? ' active-borderline' : '');
  document.getElementById('btn-bad').className        = 'val-btn' + (label === 'BAD'        ? ' active-bad'        : '');
  document.getElementById('val-comment').value = v ? (v.comment || '') : '';
}}

function exportValidations() {{
  const results = pairs.map((p, i) => ({{
    index:          i,
    pair_type:      p.pair_type,
    section_title:  p.section_title,
    bulletin_id:    p.bulletin_id,
    label:          validations[i] ? validations[i].label   : '',
    comment:        validations[i] ? validations[i].comment : '',
    original_text:  p.text || null,
    edited_text:    (validations[i] && validations[i].edited_text !== undefined) ? validations[i].edited_text : null,
    original_table:      p.table ? p.table.rows : null,
    original_table_html: p.table ? (p.table.table_html !== undefined ? p.table.table_html : rowsToHtml(p.table.rows || [])) : null,
    edited_table_html:   (validations[i] && validations[i].edited_table_html !== undefined) ? validations[i].edited_table_html : null,
  }}));
  const json = JSON.stringify(results, null, 2);
  document.getElementById('export-text').value = json;
  document.getElementById('export-overlay').style.display = 'block';
}}
function saveHTML() {{
  const src = document.documentElement.outerHTML;
  const injected = src.replace(
    'const validations = {{}};',
    'const validations = ' + JSON.stringify(validations) + ';'
  );
  const blob = new Blob([injected], {{type: 'text/html'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bsv_reviewer_validated.html';
  a.click();
}}

function exportFinalGT() {{
  const records = [];
  pairs.forEach((p, i) => {{
    const v = validations[i];
    if (!v || v.label !== 'GOOD') return;
    const originalTableHtml = p.table
      ? (p.table.table_html !== undefined ? p.table.table_html : rowsToHtml(p.table.rows || []))
      : null;
    records.push({{
      index: i,
      pair_type: p.pair_type,
      bulletin_id: p.bulletin_id,
      section_title: p.section_title,
      text: (v.edited_text !== undefined && v.edited_text !== null) ? v.edited_text : p.text,
      table_html: (v.edited_table_html !== undefined && v.edited_table_html !== null)
        ? v.edited_table_html
        : originalTableHtml,
    }});
  }});

  const jsonl = records.map(r => JSON.stringify(r)).join('\\n');
  const blob = new Blob([jsonl], {{type: 'application/x-ndjson'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bsv_gt_texttable_final.jsonl';
  a.click();

  console.log(`Exported ${{records.length}} GOOD pairs to bsv_gt_texttable_final.jsonl`);
}}

function pass(v, good) {{
  const cls  = good ? 'h-pass' : 'h-fail';
  const icon = good ? '✓' : '✗';
  return `<span class="${{cls}}">${{icon}} ${{v}}</span>`;
}}

function render() {{
  const p = pairs[cur];
  document.getElementById('counter').textContent = `${{cur + 1}} / ${{pairs.length}}`;
  document.getElementById('badge-type').textContent  = p.pair_type.replace(/_/g,' ').toUpperCase();
  document.getElementById('section-title').textContent = p.section_title || '—';

  const setMeta = (id, v) => {{ document.getElementById(id).innerHTML = v || "—"; }};
  const burl = p.url;
  document.getElementById('m-bulletin').innerHTML = burl
    ? '<a href="' + burl + '" target="_blank" style="color:#000;font-weight:bold;word-break:break-all">' + p.bulletin_id + '</a>'
    : '<b>' + p.bulletin_id + '</b>';
  setMeta('m-crop',     p.crop);
  setMeta('m-date',     p.date);
  setMeta('m-year',     p.year);
  setMeta('m-region',   p.region_code);

  const titleLen = (p.section_title || '').length;
  document.getElementById('h-section').innerHTML  = p.section_title === 'Document'
    ? '<span class="h-fail">✗ Document (structure detection failed)</span>'
    : '<span class="h-pass">✓ ' + (p.section_title || '—') + '</span>';
  document.getElementById('h-title-len').innerHTML = pass(titleLen + ' chars', titleLen >= 3);
  document.getElementById('h-section-level').textContent = p.section_level
  ? 'Level ' + p.section_level + (p.section_level === 1 ? ' (main)' : ' (subsection)')
  : '—';

  // Text (editable)
  const hasTxt = p.text && p.text.trim().length > 0;
  document.getElementById('text-section').style.display    = hasTxt ? '' : 'none';
  document.getElementById('text-heuristics').style.display = hasTxt ? '' : 'none';
  if (hasTxt) {{
    const savedText = validations[cur] ? validations[cur].edited_text : undefined;
    document.getElementById('text-box').value = (savedText !== undefined && savedText !== null) ? savedText : p.text;
    const minLen = p.pair_type === 'text_table' ? 100 : 50;
    document.getElementById('h-txt-len').innerHTML =
      pass(p.text.length + ' chars', p.text.length >= minLen);
  }}

  // Image
  const hasImg = !!p.image;
  document.getElementById('image-section').style.display   = hasImg ? '' : 'none';
  document.getElementById('img-heuristics').style.display  = hasImg ? '' : 'none';
  if (hasImg) {{
    const fname  = p.image.filename || '';
    const imgSrc = p.img_b64 || '';
    document.getElementById('image-box').innerHTML = imgSrc
      ? `<img src="${{imgSrc}}" alt="${{fname}}">`
      : `<div style="padding:8px;background:#f5f5f5;color:#888;font-size:11px">Image not found: ${{fname}}</div>`;
    document.getElementById('caption-box').textContent       = p.image.caption || '';
    document.getElementById('h-img-fname').textContent       = fname;
    document.getElementById('h-img-page').textContent        = p.img_page ?? '—';
    document.getElementById('h-img-caption').textContent     = p.image.caption || '—';
    if (p.img_width && p.img_height) {{
      document.getElementById('h-img-dims').textContent  = `${{p.img_width}} × ${{p.img_height}} px`;
      document.getElementById('h-img-area').innerHTML    = pass(p.img_area.toLocaleString() + ' px²', p.img_area >= 200000);
      document.getElementById('h-img-ratio').innerHTML   = pass(p.img_ratio, p.img_ratio < 8);
    }} else {{
      document.getElementById('h-img-dims').textContent  = '—';
      document.getElementById('h-img-area').textContent  = '—';
      document.getElementById('h-img-ratio').textContent = '—';
    }}
  }}

  // Table (editable)
  const hasTbl = !!p.table;
  document.getElementById('table-section').style.display   = hasTbl ? '' : 'none';
  document.getElementById('tbl-heuristics').style.display  = hasTbl ? '' : 'none';
  if (hasTbl) {{
    const savedHtml = validations[cur] ? validations[cur].edited_table_html : undefined;
    const originalHtml = p.table.table_html !== undefined ? p.table.table_html : rowsToHtml(p.table.rows || []);
    const rawHtml = savedHtml !== undefined ? savedHtml : originalHtml;
    document.getElementById('table-box').value = rawHtml;
    document.getElementById('table-preview').innerHTML =
      '<table class="data-table"><tbody>' + rawHtml + '</tbody></table>';
    document.getElementById('h-tbl-page').textContent  = p.table.page ?? '—';
    document.getElementById('h-tbl-rows').innerHTML    = pass(p.tbl_rows + ' rows', p.tbl_rows >= 3);
    document.getElementById('h-tbl-cols').innerHTML    = pass(p.tbl_cols + ' cols', p.tbl_cols >= 2);
    document.getElementById('h-tbl-empty').innerHTML   = pass(p.tbl_empty_pct + '% empty', p.tbl_empty_pct <= 50);
    const vrules = p.table.vertical_rules;
    document.getElementById("h-tbl-vrules").textContent = vrules ? `${{vrules.length}} detected (${{p.tbl_cols ?? '?'}} columns extracted)` : "—";
  }}

  renderValButtons();
  window.scrollTo(0, 0);
}}

function go(dir) {{
  cur = Math.max(0, Math.min(pairs.length - 1, cur + dir));
  render();
}}

function toggleRef() {{
  document.getElementById('ref-overlay').classList.toggle('visible');
}}

document.addEventListener('keydown', e => {{
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight') go(1);
  if (e.key === 'ArrowLeft')  go(-1);
  if (e.key === '1') setVal('GOOD');
  if (e.key === '2') setVal('BORDERLINE');
  if (e.key === '3') setVal('BAD');
  if (e.key === 'Escape') document.getElementById('ref-overlay').classList.remove('visible');
}});

render();
</script>
</body>
</html>"""

with open(HTML_OUT, "w", encoding="utf-8") as f:
    f.write(HTML)

print(f"Generated : {HTML_OUT}")
print(f"Pairs     : {len(pairs)}")
print(f"Keyboard  : ← → navigate | 1 GOOD | 2 BORDERLINE | 3 BAD | Esc close reference")
