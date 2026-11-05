"""Lossless JSONL text recipes referencing PyMuPDF raw character streams.

Offsets are Unicode code-point offsets into all rawdict characters on a page,
in block/line/span/character order. Literal parts preserve characters which
cannot be copied exactly from those source characters.
"""

import hashlib


RECIPE_VERSION = 1
EXTRACTION = "pymupdf-rawdict-characters-v1"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_data(page, text_page=None):
    """Return source text, glyph offsets and trimmed formatting spans."""
    text_parts, glyphs, spans, boundaries = [], [], [], set()
    offset = 0
    for block in page.get_text("rawdict", textpage=text_page)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                chars = span["chars"]
                span_start = offset
                for char in chars:
                    value = char["c"]
                    text_parts.append(value)
                    glyphs.append((offset, offset + len(value), tuple(char["bbox"])))
                    offset += len(value)
                start, end = 0, len(chars)
                while start < end and chars[start]["c"].isspace():
                    start += 1
                while end > start and chars[end - 1]["c"].isspace():
                    end -= 1
                trimmed = chars[start:end]
                if trimmed:
                    boundaries.add(span_start + sum(len(c["c"]) for c in chars[:start]))
                    boundaries.add(span_start + sum(len(c["c"]) for c in chars[:end]))
                    spans.append((
                        min(c["bbox"][0] for c in trimmed), min(c["bbox"][1] for c in trimmed),
                        max(c["bbox"][2] for c in trimmed), max(c["bbox"][3] for c in trimmed),
                        "".join(c["c"] for c in trimmed),
                    ))
    return "".join(text_parts), glyphs, spans, boundaries


def segments_in_bbox(page_text, glyphs, page_index, bbox):
    """Yield contiguous source-character ranges whose glyph centers lie in bbox."""
    x0, y0, x1, y1 = bbox
    begin = end = None
    for start, stop, (gx0, gy0, gx1, gy1) in glyphs:
        cx, cy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
        inside = x0 - 0.5 <= cx <= x1 + 0.5 and y0 - 0.5 <= cy <= y1 + 0.5
        if inside and begin is not None and start == end:
            end = stop
        else:
            if begin is not None:
                yield page_index, begin, page_text[begin:end]
            begin, end = (start, stop) if inside else (None, None)
    if begin is not None:
        yield page_index, begin, page_text[begin:end]


def is_word_char(char):
    return bool(char) and char.isalnum()


def build_recipe(text, locator, pages, glyphs, span_boundaries, pdf_sha256):
    """Build exact source slices plus literals, then verify the round trip.

    Long exact substrings are preferred; short whole words may also be cited.
    This avoids inventing provenance by stitching arbitrary single letters.
    """
    indices, boxes = locator["page_index"], locator["bbox"]
    if isinstance(indices, int):
        indices, boxes = [indices], [boxes]
    segments = [
        segment for page, box in zip(indices, boxes)
        for segment in segments_in_bbox(pages[page], glyphs[page], page, box)
    ]
    parts = []
    offset = 0
    previous = None
    while offset < len(text):
        remaining = len(text) - offset
        candidates = []
        if remaining >= 4:
            anchor = text[offset:offset + 4]
            for page, base, source in segments:
                pos = source.find(anchor)
                while pos != -1:
                    low, high = 4, min(remaining, len(source) - pos)
                    while low < high:
                        middle = (low + high + 1) // 2
                        if source.startswith(text[offset:offset + middle], pos):
                            low = middle
                        else:
                            high = middle - 1
                    candidates.append((low, page, base + pos))
                    pos = source.find(anchor, pos + 1)
        # Cite a short complete word only when word boundaries agree. Do not
        # manufacture provenance by collecting arbitrary characters.
        if not candidates and is_word_char(text[offset]) and (offset == 0 or not is_word_char(text[offset - 1])):
            end = offset
            while end < len(text) and is_word_char(text[end]):
                end += 1
            if end - offset <= 3:
                token = text[offset:end]
                for page, base, source in segments:
                    pos = source.find(token)
                    while pos != -1:
                        before = source[pos - 1] if pos else ""
                        after = source[pos + len(token)] if pos + len(token) < len(source) else ""
                        if ((not is_word_char(before) or base + pos in span_boundaries[page])
                                and (not is_word_char(after) or base + pos + len(token) in span_boundaries[page])):
                            candidates.append((len(token), page, base + pos))
                        pos = source.find(token, pos + 1)
        if candidates and not is_word_char(text[offset]) and offset + 1 < len(text) and is_word_char(text[offset + 1]):
            longest = max(length for length, _, _ in candidates)
            # A PDF line break may remove the source space before a word. A
            # longer but partial match elsewhere should not force the end of
            # that word into a content literal; keep the separator as literal
            # and match the complete word on the next iteration instead.
            if (offset + longest < len(text) and is_word_char(text[offset + longest - 1])
                    and is_word_char(text[offset + longest])):
                candidates = []
        if candidates:
            longest = max(length for length, _, _ in candidates)
            split = offset + longest
            if split < len(text) and is_word_char(text[split - 1]) and is_word_char(text[split]):
                word_start, word_end = split, split
                while word_start > offset and is_word_char(text[word_start - 1]):
                    word_start -= 1
                while word_end < len(text) and is_word_char(text[word_end]):
                    word_end += 1
                token = text[word_start:word_end]
                # Do not consume only a prefix of a word when the complete word
                # is elsewhere in the source region (e.g. 2019 versus 2018).
                if word_start > offset and any(token in source for _, _, source in segments):
                    length = word_start - offset
                    candidates = [(length, page, start) for size, page, start in candidates if size >= length]
        if candidates:
            longest = max(length for length, _, _ in candidates)
            candidates = [candidate for candidate in candidates if candidate[0] == longest]
            # Prefer a contiguous continuation, then a nearby position on the
            # previous page; otherwise use a deterministic PDF reading order.
            def rank(candidate):
                _, page, start = candidate
                if previous is None:
                    return (1, 1, page, start)
                prev_page, prev_end = previous
                return (0 if (page, start) == previous else 1,
                        0 if page == prev_page else 1,
                        abs(start - prev_end) if page == prev_page else page, start)
            length, page, start = min(candidates, key=rank)
            end = start + length
            if parts and parts[-1].get("page_index") == page and parts[-1].get("end") == start:
                parts[-1]["end"] = end
            else:
                parts.append({"page_index": page, "start": start, "end": end})
            previous = (page, end)
            offset += length
        else:
            if parts and "literal" in parts[-1]:
                parts[-1]["literal"] += text[offset]
            else:
                parts.append({"literal": text[offset]})
            offset += 1
    literal_alnum = sum(char.isalnum() for part in parts for char in part.get("literal", ""))
    recipe = {
        "version": RECIPE_VERSION,
        "extraction": EXTRACTION,
        "pdf_sha256": pdf_sha256,
        "text_sha256": text_sha256(text),
        "literal_alnum_count": literal_alnum,
        "parts": parts,
    }
    if restore_recipe(recipe, pages) != text:
        raise ValueError("text reconstruction recipe failed its round-trip check")
    return recipe


def restore_recipe(recipe, pages):
    """Recreate text and reject malformed or non-matching recipes."""
    if not isinstance(recipe, dict) or recipe.get("version") != RECIPE_VERSION or recipe.get("extraction") != EXTRACTION:
        raise ValueError("unsupported or invalid text reconstruction recipe")
    parts = recipe.get("parts")
    if not isinstance(parts, list):
        raise ValueError("text reconstruction parts must be a list")
    result = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("invalid text reconstruction part")
        if set(part) == {"literal"} and isinstance(part["literal"], str):
            result.append(part["literal"])
            continue
        if set(part) != {"page_index", "start", "end"}:
            raise ValueError("invalid source character reference")
        page, start, end = (part[key] for key in ("page_index", "start", "end"))
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (page, start, end)):
            raise ValueError("character reference offsets must be integers")
        if page < 0 or page >= len(pages) or start < 0 or end <= start or end > len(pages[page]):
            raise ValueError("character reference is outside the PDF page")
        result.append(pages[page][start:end])
    text = "".join(result)
    if text_sha256(text) != recipe.get("text_sha256"):
        raise ValueError("reconstructed text does not match its SHA-256 digest")
    return text
