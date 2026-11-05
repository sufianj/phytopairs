"""Add source-PDF text locators and partial-reproduction flags to JSONL records."""

import argparse
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

try:
    import pymupdf as fitz
except ImportError:
    sys.exit("PyMuPDF is required: python -m pip install 'PyMuPDF>=1.24'")

from text_reconstruction import build_recipe, file_sha256, page_data
from docling_fallback import DoclingFallback, DoclingFallbackError


DEFAULT_PDF_DIRS = (Path(__file__).resolve().parents[1] / "data" / "local" / "pdfs",)
PARTIAL_PHRASE = "Reproduction partielle autorisée"


class TextNotFound(ValueError):
    """The full text was not found in the PDF text layer."""


def normalize(text):
    """Normalize PDF ligatures, accents, case and punctuation for matching."""
    text = text.casefold().replace("œ", "oe").replace("æ", "ae")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if c.isalnum())


def records_in(path):
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path}, line {number}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected an object in {path}, line {number}")
            yield number, record


def valid_id(value):
    return isinstance(value, str) and bool(value) and value not in {".", ".."} and Path(value).name == value


def find_pdfs(directories, bulletin_ids):
    """Find exact stems recursively, preferring earlier directories and paths."""
    found = {}
    for directory in directories:
        if not directory.is_dir():
            print(f"Warning: PDF directory does not exist: {directory}", file=sys.stderr)
            continue
        remaining = bulletin_ids - found.keys()
        if not remaining:
            break
        # Most source PDFs are directly in the root. Search subdirectories only
        # for IDs not already found in this root.
        for bulletin_id in remaining:
            path = directory / (bulletin_id + ".pdf")
            if path.is_file():
                found[bulletin_id] = path
        remaining = bulletin_ids - found.keys()
        if remaining:
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() == ".pdf" and path.stem in remaining and path.is_file():
                    found[path.stem] = path
                    remaining.remove(path.stem)
                    if not remaining:
                        break
    return found


def word_stream(words):
    """Return normalized characters and their originating word indexes."""
    pieces, indexes = [], []
    for index, word in enumerate(words):
        piece = normalize(word[4])
        pieces.append(piece)
        indexes.extend([index] * len(piece))
    return "".join(pieces), indexes


def page_number_indexes(words, page_height, number):
    """Find the printed page number in a page's header or footer margin."""
    if not number:
        return set()
    return {
        index for index, word in enumerate(words)
        if normalize(word[4]) == number
        and (word[1] <= page_height * 0.10 or word[3] >= page_height * 0.90)
    }


def fragmented_matches(query, stream, indexes):
    """Find the complete query in ordered chunks, permitting intervening words.

    Long anchors and bounded gaps prevent a loose character-subsequence match.
    This handles unrelated PDF material between text blocks, not missing or
    edited query text. Results are still subject to uniqueness checks.
    """
    if len(query) < 64:
        return []
    anchor = 32
    results = set()
    examined = 0
    start = stream.find(query[:anchor])
    while start != -1:
        if start and indexes[start - 1] == indexes[start]:
            start = stream.find(query[:anchor], start + 1)
            continue
        pending = [(start, 0, 0, 0)]
        while pending:
            pos, offset, gaps, skipped = pending.pop()
            examined += 1
            if examined > 10000:
                raise TextNotFound("too many possible fragment alignments")
            # Find the longest query prefix at this position.
            low, high = 0, min(len(query) - offset, len(stream) - pos)
            while low < high:
                mid = (low + high + 1) // 2
                if stream.startswith(query[offset : offset + mid], pos):
                    low = mid
                else:
                    high = mid - 1
            pos += low
            offset += low
            if offset == len(query):
                if pos == len(stream) or indexes[pos - 1] != indexes[pos]:
                    results.add((start, pos - 1))
                continue
            # Only skip complete PDF words; require substantial matching
            # anchors after each gap, except for the final short suffix.
            if not low or pos == len(stream) or indexes[pos - 1] == indexes[pos] or gaps == 6:
                continue
            size = min(anchor, len(query) - offset)
            next_pos = stream.find(query[offset : offset + size], pos, min(len(stream), pos + 1000 + size))
            while next_pos != -1 and next_pos - pos <= 1000:
                new_skipped = skipped + next_pos - pos
                if (next_pos == 0 or indexes[next_pos - 1] != indexes[next_pos]) and new_skipped <= 1500:
                    pending.append((next_pos, offset, gaps + 1, new_skipped))
                next_pos = stream.find(query[offset : offset + size], next_pos + 1, min(len(stream), pos + 1000 + size))
        start = stream.find(query[:anchor], start + 1)
    return sorted(results)


def reordered_matches(query, stream, indexes, leading_marker=False, leading_number=None, number_indexes=None):
    """Cover the entire query with exact chunks in any PDF order.

    The original parser concatenates separately collected spans around an
    image. Those groups can appear in a different order from the PDF stream.
    Require long exact anchors and word boundaries; never invent missing text.
    """
    if len(query) < 32:
        return []
    results = set()
    pending = [(0, (), 0, 0)]  # query offset, source ranges, short chunks and characters
    examined = 0
    while pending:
        offset, ranges, short_chunks, short_chars = pending.pop()
        examined += 1
        if examined > 10000:
            raise TextNotFound("too many possible reordered text alignments")
        if offset == len(query):
            results.add(ranges)
            continue
        if len(ranges) >= 20:
            continue
        remaining = len(query) - offset
        if offset == 0 and leading_number:
            anchor = len(leading_number)
        elif offset == 0 and leading_marker:
            anchor = 1
        else:
            anchor = min(8, remaining)
        # The bulk of the query must still have long exact anchors.
        if anchor < 8 and not (
            (offset == 0 and (leading_marker or leading_number)) or (offset >= 32 and remaining < 8)
        ):
            continue
        candidates = []
        anchors = [anchor]
        if anchor == 8 and not (offset == 0 and (leading_marker or leading_number)):
            # Text selected around a table or font change can contain short,
            # exact fragments between much longer spans.
            anchors.extend((7, 6, 5, 4))
        for anchor in anchors:
            pos = stream.find(query[offset : offset + anchor])
            while pos != -1:
                if pos == 0 or indexes[pos - 1] != indexes[pos]:
                    low, high = anchor, min(remaining, len(stream) - pos)
                    while low < high:
                        mid = (low + high + 1) // 2
                        if stream.startswith(query[offset : offset + mid], pos):
                            low = mid
                        else:
                            high = mid - 1
                    while (low and offset + low < len(query) and pos + low < len(stream)
                           and indexes[pos + low - 1] == indexes[pos + low]):
                        low -= 1
                    if low >= anchor:
                        first, last = indexes[pos], indexes[pos + low - 1]
                        # An isolated leading "o" is a bullet retained by the
                        # parser. It must occur close to the following text.
                        marker_nearby = not (
                            leading_marker and offset == 1 and len(ranges) == 1
                            and not (ranges[0][1] < first <= ranges[0][1] + 8)
                        )
                        # A manually constructed record can repeat the same
                        # passage; permit reuse only from the same starting word.
                        overlap_ok = all(last < a or first > b or first == a for a, b in ranges)
                        number_ok = not (
                            offset == 0 and leading_number and low == len(leading_number)
                            and (first != last or first not in (number_indexes or set()))
                        )
                        small = low < 8 and not (offset == 0 and (leading_marker or leading_number))
                        within_short_limit = not small or (
                            short_chunks < 6 and short_chars + low <= max(16, len(query) // 20)
                        )
                        if marker_nearby and overlap_ok and number_ok and within_short_limit:
                            candidates.append((low, first, last))
                pos = stream.find(query[offset : offset + anchor], pos + 1)
            if candidates:
                break
        # Prefer the longest continuous evidence at each step, but keep all
        # equally long occurrences so ambiguity can be resolved geometrically.
        if candidates:
            longest = max(item[0] for item in candidates)
            for length, first, last in candidates:
                if length == longest:
                    small = length < 8 and not (offset == 0 and (leading_marker or leading_number))
                    pending.append((offset + length, ranges + ((first, last),),
                                    short_chunks + int(small), short_chars + (length if small else 0)))
    return sorted(results)


def asset_hint(record, page_count):
    """Return a 0-based asset page and PDF rectangle when available."""
    kind = "table" if record.get("pair_type") == "text_table" else "image"
    asset = record.get(kind)
    asset = asset if isinstance(asset, dict) else {}
    raw_bbox = asset.get("bbox", record.get("bbox"))
    if isinstance(raw_bbox, dict) and "bbox" in raw_bbox:
        bbox_meta = raw_bbox
        raw_bbox = bbox_meta.get("bbox")
    else:
        bbox_meta = {}
    page = None
    for obj in (asset, bbox_meta, record):
        try:
            if obj.get("page_index") is not None and not isinstance(obj["page_index"], bool):
                page = int(obj["page_index"])
                break
            if obj.get("page") is not None and not isinstance(obj["page"], bool):
                page = int(obj["page"]) - 1
                break
        except (TypeError, ValueError, OverflowError):
            continue
    if page is not None and not 0 <= page < page_count:
        page = None
    if raw_bbox is None and all(k in asset for k in ("left", "top", "right", "bottom")):
        raw_bbox = [asset[k] for k in ("left", "top", "right", "bottom")]
    if isinstance(raw_bbox, dict):
        raw_bbox = [raw_bbox.get(k) for k in ("left", "top", "right", "bottom")]
    try:
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
            raise ValueError("invalid bbox")
        box = tuple(float(value) for value in raw_bbox)
        if not all(math.isfinite(value) for value in box) or box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("invalid bbox")
    except (TypeError, ValueError, OverflowError):
        box = None
    return page, box


def nearest_match(matches, asset_page, asset_box, with_count=False):
    """Use proximity, then choose the first remaining location in reading order."""
    candidates = set(matches)
    if asset_page is not None:
        closest_page = min(abs(page - asset_page) for page, _ in candidates)
        candidates = {match for match in candidates if abs(match[0] - asset_page) == closest_page}
    pages = {page for page, _ in candidates}
    if asset_box is not None and len(pages) == 1 and (asset_page is None or pages == {asset_page}):
        ax0, ay0, ax1, ay1 = asset_box
        def distance(match):
            _, (x0, y0, x1, y1) = match
            dx = max(ax0 - x1, x0 - ax1, 0)
            dy = max(ay0 - y1, y0 - ay1, 0)
            center_dx = (x0 + x1 - ax0 - ax1) / 2
            center_dy = (y0 + y1 - ay0 - ay1) / 2
            return (dx * dx + dy * dy, center_dx * center_dx + center_dy * center_dy)
        best = min(distance(match) for match in candidates)
        candidates = {match for match in candidates if distance(match) == best}
    chosen = min(candidates, key=lambda match: (match[0], match[1][1], match[1][0], match[1][3], match[1][2]))
    return (chosen, len(candidates)) if with_count else chosen


def nearest_multipage_match(matches, asset_page, asset_box, with_count=False):
    """Resolve multipage matches by the asset's nearest page and rectangle."""
    candidates = set(matches)
    if asset_page is not None:
        best = min(min(abs(page - asset_page) for page in pages) for pages, _ in candidates)
        candidates = {item for item in candidates if min(abs(p - asset_page) for p in item[0]) == best}
    if asset_page is not None and asset_box is not None and all(asset_page in pages for pages, _ in candidates):
        ax0, ay0, ax1, ay1 = asset_box
        def distance(item):
            pages, boxes = item
            x0, y0, x1, y1 = boxes[pages.index(asset_page)]
            dx, dy = max(ax0 - x1, x0 - ax1, 0), max(ay0 - y1, y0 - ay1, 0)
            cx, cy = (x0 + x1 - ax0 - ax1) / 2, (y0 + y1 - ay0 - ay1) / 2
            return (dx * dx + dy * dy, cx * cx + cy * cy)
        best = min(distance(item) for item in candidates)
        candidates = {item for item in candidates if distance(item) == best}
    chosen = min(candidates, key=lambda match: (
        match[0][0], match[1][0][1], match[1][0][0], match[0], match[1],
    ))
    return (chosen, len(candidates)) if with_count else chosen


class PdfData:
    def __init__(self, path):
        self.path = path
        self._sha256 = None
        self.pages = []
        self.page_heights = []
        self.page_widths = []
        self.page_texts = []
        self.page_glyphs = []
        self.page_span_boundaries = []
        phrase_text = []
        with fitz.open(path) as document:
            if document.is_encrypted:
                raise ValueError("PDF is encrypted")
            for page in document:
                self.page_heights.append(page.rect.height)
                self.page_widths.append(page.rect.width)
                text_page = page.get_textpage()
                phrase_text.append(page.get_text("text", textpage=text_page))
                streams = []
                for sort in (False, True):
                    words = page.get_text("words", sort=sort, textpage=text_page)
                    text, indexes = word_stream(words)
                    streams.append((words, text, indexes))
                # The original parser concatenates PyMuPDF text spans. Font
                # changes can split a single word into independently selected
                # spans, which the word extractor merges and cannot locate.
                page_text, page_glyphs, spans, boundaries = page_data(page, text_page)
                self.page_texts.append(page_text)
                self.page_glyphs.append(page_glyphs)
                self.page_span_boundaries.append(boundaries)
                text, indexes = word_stream(spans)
                streams.append((spans, text, indexes))
                self.pages.append(streams)
        extracted = normalize("\n".join(phrase_text))
        self.partial_reproduction = normalize(PARTIAL_PHRASE) in extracted if extracted else None

    @property
    def sha256(self):
        if self._sha256 is None:
            self._sha256 = file_sha256(self.path)
        return self._sha256

    def reconstructible_matches(self, matches, text, asset_page, multipage=False):
        """Prefer nearby candidates requiring the fewest content literals."""
        candidates = set(matches)
        if len(candidates) < 2:
            return candidates
        if asset_page is not None:
            def page_distance(match):
                pages = match[0] if multipage else (match[0],)
                return min(abs(page - asset_page) for page in pages)
            distance = min(page_distance(match) for match in candidates)
            candidates = {match for match in candidates if page_distance(match) == distance}
        if len(candidates) < 2:
            return candidates
        scores = {}
        for pages, boxes in candidates:
            locator = (
                {"page_index": list(pages), "bbox": [list(box) for box in boxes]}
                if multipage else {"page_index": pages, "bbox": list(boxes)}
            )
            recipe = build_recipe(
                text, locator, self.page_texts, self.page_glyphs,
                self.page_span_boundaries, self.sha256,
            )
            scores[(pages, boxes)] = recipe["literal_alnum_count"]
        best = min(scores.values())
        return {match for match, score in scores.items() if score == best}

    def locate(self, text, asset_page=None, asset_box=None):
        query = normalize(text)
        if not query:
            raise ValueError("text is empty after normalization")
        matches = {}
        leading_marker = bool(re.match(r"^\s*[oO]\s+", text))
        number_match = re.match(r"^\s*(\d{1,3})\s+", text)
        leading_number = number_match.group(1) if number_match else None
        fragmented = []
        for page_index, streams in enumerate(self.pages):
            for words, stream, indexes in streams:
                start = stream.find(query)
                while start != -1:
                    end = start + len(query) - 1
                    # Reject substring hits inside a PDF word.
                    if (start == 0 or indexes[start - 1] != indexes[start]) and (
                        end + 1 == len(stream) or indexes[end + 1] != indexes[end] or len(query) >= 32
                    ):
                        selected = words[indexes[start] : indexes[end] + 1]
                        box = (
                            min(w[0] for w in selected), min(w[1] for w in selected),
                            max(w[2] for w in selected), max(w[3] for w in selected),
                        )
                        matches[(page_index, box)] = False
                    start = stream.find(query, start + 1)
                fragmented.append((page_index, words, stream, indexes))
        # Consider fragmented locations even when there is a contiguous hit:
        # otherwise an unrelated exact duplicate could silently win.
        for page_index, words, stream, indexes in fragmented:
            for start, end in fragmented_matches(query, stream, indexes):
                selected = words[indexes[start] : indexes[end] + 1]
                box = (
                    min(w[0] for w in selected), min(w[1] for w in selected),
                    max(w[2] for w in selected), max(w[3] for w in selected),
                )
                matches[(page_index, box)] = False
        # Some image local_text values concatenate several geometrically
        # collected groups in a different order from the PDF. If there is no
        # asset box, prefer stronger ordered evidence over speculative mixes
        # of reordered chunks; otherwise also check for nearer alternatives.
        if not matches or asset_box is not None:
            for page_index, words, stream, indexes in fragmented:
                number_indexes = page_number_indexes(words, self.page_heights[page_index], leading_number)
                for ranges in reordered_matches(query, stream, indexes, leading_marker, leading_number, number_indexes):
                    selected = [word for first, last in ranges for word in words[first : last + 1]]
                    box = (
                        min(w[0] for w in selected), min(w[1] for w in selected),
                        max(w[2] for w in selected), max(w[3] for w in selected),
                    )
                    match = (page_index, box)
                    reused = any(
                        not (last < a or first > b)
                        for i, (first, last) in enumerate(ranges) for a, b in ranges[:i]
                    )
                    matches[match] = matches.get(match, True) and reused
        if not matches:
            return self.locate_multipage(text, query, leading_marker, leading_number, asset_page, asset_box)
        candidates = self.reconstructible_matches(matches, text, asset_page)
        (page_index, box), tied = nearest_match(candidates, asset_page, asset_box, with_count=True)
        result = {"page_index": page_index, "bbox": list(box)}
        if tied > 1:
            result["ambiguous_match_count"] = tied
        if matches[(page_index, box)]:
            result["reused_source_text"] = True
        return result

    def locate_multipage(self, text, query, leading_marker, leading_number, asset_page, asset_box):
        """Match complete text across consecutive pages, keeping per-page boxes."""
        matches = {}
        for sort in (0, 1, 2):
            words, word_pages = [], []
            for page_index, streams in enumerate(self.pages):
                page_words = streams[sort][0]
                words.extend(page_words)
                word_pages.extend([page_index] * len(page_words))
            stream, indexes = word_stream(words)

            def add(ranges):
                # The matching order may vary within a page, but pages must
                # follow the document order and contain no skipped pages.
                sequence = [page for first, last in ranges for page in (word_pages[first], word_pages[last])]
                if sequence != sorted(sequence):
                    return
                selected = {}
                for first, last in ranges:
                    for index in range(first, last + 1):
                        selected.setdefault(word_pages[index], []).append(words[index])
                pages = tuple(sorted(selected))
                if len(pages) < 2 or pages != tuple(range(pages[0], pages[-1] + 1)):
                    return
                boxes = tuple(
                    (min(w[0] for w in selected[p]), min(w[1] for w in selected[p]),
                     max(w[2] for w in selected[p]), max(w[3] for w in selected[p]))
                    for p in pages
                )
                reused = any(
                    not (last < a or first > b)
                    for i, (first, last) in enumerate(ranges) for a, b in ranges[:i]
                )
                key = (pages, boxes)
                matches[key] = matches.get(key, True) and reused

            start = stream.find(query)
            while start != -1:
                end = start + len(query) - 1
                if (start == 0 or indexes[start - 1] != indexes[start]) and (
                    end + 1 == len(stream) or indexes[end + 1] != indexes[end] or len(query) >= 32
                ):
                    add(((indexes[start], indexes[end]),))
                start = stream.find(query, start + 1)
            number_indexes = set()
            if leading_number:
                number_indexes = {
                    index for index, word in enumerate(words)
                    if normalize(word[4]) == leading_number
                    and (word[1] <= self.page_heights[word_pages[index]] * 0.10
                         or word[3] >= self.page_heights[word_pages[index]] * 0.90)
                }
            for ranges in reordered_matches(query, stream, indexes, leading_marker, leading_number, number_indexes):
                add(ranges)
        if not matches:
            raise TextNotFound("text not found as a complete match on one page or consecutive pages")
        candidates = self.reconstructible_matches(matches, text, asset_page, multipage=True)
        (pages, boxes), tied = nearest_multipage_match(candidates, asset_page, asset_box, with_count=True)
        result = {"page_index": list(pages), "bbox": [list(box) for box in boxes]}
        if tied > 1:
            result["ambiguous_match_count"] = tied
        if matches[(pages, boxes)]:
            result["reused_source_text"] = True
        return result

    def locate_partial(self, text, asset_page=None, asset_box=None):
        """Find a substantial exact prefix; unmatched content stays literal.

        This is only a partial source trace, not evidence that the remaining
        text appears in the PDF. It is used only when storing literals is
        explicitly allowed or the original text is already being kept.
        """
        query = normalize(text)
        if len(query) < 128:
            raise TextNotFound("text too short for a partial source match")
        anchor = query[:64]
        matches = {}
        for page_index, streams in enumerate(self.pages):
            for words, stream, indexes in streams:
                start = stream.find(anchor)
                while start != -1:
                    if start == 0 or indexes[start - 1] != indexes[start]:
                        low, high = len(anchor), min(len(query), len(stream) - start)
                        while low < high:
                            mid = (low + high + 1) // 2
                            if stream.startswith(query[:mid], start):
                                low = mid
                            else:
                                high = mid - 1
                        if low >= 80 and low * 4 >= len(query):
                            selected = words[indexes[start]:indexes[start + low - 1] + 1]
                            box = (
                                min(w[0] for w in selected), min(w[1] for w in selected),
                                max(w[2] for w in selected), max(w[3] for w in selected),
                            )
                            key = (page_index, box)
                            matches[key] = max(matches.get(key, 0), low)
                    start = stream.find(anchor, start + 1)
        if not matches:
            raise TextNotFound("no sufficiently long partial source match")
        if asset_page is not None:
            closest = min(abs(page - asset_page) for page, _ in matches)
            matches = {key: value for key, value in matches.items() if abs(key[0] - asset_page) == closest}
        longest = max(matches.values())
        candidates = {key for key, value in matches.items() if value == longest}
        (page_index, box), tied = nearest_match(candidates, asset_page, asset_box, with_count=True)
        result = {
            "page_index": page_index, "bbox": list(box), "partial_source_match": True,
            "matched_normalized_characters": longest, "total_normalized_characters": len(query),
        }
        if tied > 1:
            result["ambiguous_match_count"] = tied
        return result


class PdfCache:
    def __init__(self, pdfs, size=8):
        self.pdfs = pdfs
        self.size = size
        self.cache = OrderedDict()

    def get(self, bulletin_id):
        if bulletin_id not in self.cache:
            path = self.pdfs.get(bulletin_id)
            if path is None:
                raise ValueError(f"PDF not found for bulletin_id {bulletin_id!r}")
            # Cache failures too, so a broken PDF is not opened for every pair.
            try:
                self.cache[bulletin_id] = PdfData(path)
            except Exception as exc:
                self.cache[bulletin_id] = ValueError(f"Cannot read PDF {path}: {exc}")
            if len(self.cache) > self.size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(bulletin_id)
        data = self.cache[bulletin_id]
        if isinstance(data, Exception):
            raise data
        return data


def process(input_path, output_path, directories, remove_text=False, overwrite=False,
            allow_content_literals=False, keep_authorized_text=False, keep_text_bbox=False,
            use_docling=False, docling_ocr="easyocr", docling_provider=None):
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Input and output must be different files")
    if output_path.exists() and not overwrite:
        raise ValueError(f"Output already exists: {output_path}; use --overwrite to replace it")
    if not any(directory.is_dir() for directory in directories):
        paths = ", ".join(str(directory) for directory in directories)
        raise ValueError(f"No PDF directories exist: {paths}. Specify the correct paths with --pdf-dir")
    ids = {r.get("bulletin_id") for _, r in records_in(input_path) if valid_id(r.get("bulletin_id"))}
    pdfs = find_pdfs(directories, ids)
    cache = PdfCache(pdfs)
    docling = docling_provider or (DoclingFallback(normalize, engine=docling_ocr) if use_docling else None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = failures = matched = 0
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as destination:
            temp_path = Path(destination.name)
            for number, record in records_in(input_path):
                bulletin_id = record.get("bulletin_id")
                image = record.get("image")
                image_local_text = image.pop("local_text", None) if isinstance(image, dict) else None
                record.pop("text_bbox", None)
                if keep_text_bbox:
                    record["text_bbox"] = None
                record["partial_reproduction"] = None
                try:
                    if "text_reconstruction" in record:
                        legacy_recipe = record["text_reconstruction"]
                        if "text_reproduction" in record and record["text_reproduction"] != legacy_recipe:
                            raise ValueError("conflicting text_reconstruction and text_reproduction recipes")
                        record["text_reproduction"] = record.pop("text_reconstruction")
                    if not valid_id(bulletin_id):
                        raise ValueError("missing or invalid bulletin_id")
                    data = cache.get(bulletin_id)
                    record["partial_reproduction"] = data.partial_reproduction
                    text = record.get("text")
                    if not isinstance(text, str):
                        raise ValueError("missing or invalid text")
                    page, box = asset_hint(record, len(data.pages))
                    try:
                        text_bbox = data.locate(text, page, box)
                    except TextNotFound as original_error:
                        text_bbox = None
                        if docling is not None:
                            source_hint = None
                            try:
                                hint = data.locate_partial(text, page, box)
                                source_hint = hint["page_index"]
                            except TextNotFound:
                                pass
                            try:
                                text_bbox = docling.locate(data, text, page, box, source_hint)
                                print(
                                    f"Line {number} ({bulletin_id!r}): Docling matched "
                                    f"{text_bbox['docling_match']['coverage']:.1%} of normalized text",
                                    file=sys.stderr,
                                )
                            except DoclingFallbackError as exc:
                                print(f"Line {number} ({bulletin_id!r}): warning: {exc}", file=sys.stderr)
                        if text_bbox is None:
                            literals_permitted = (
                                not (remove_text or keep_authorized_text)
                                or (keep_authorized_text and data.partial_reproduction is True)
                                or allow_content_literals
                            )
                            if not literals_permitted:
                                raise original_error
                            text_bbox = data.locate_partial(text, page, box)
                            print(
                                f"Line {number} ({bulletin_id!r}): warning: only a partial PDF text "
                                "match was found; unmatched text will be stored as literals",
                                file=sys.stderr,
                            )
                    if keep_text_bbox:
                        record["text_bbox"] = text_bbox
                    recipe = build_recipe(
                        text, text_bbox, data.page_texts, data.page_glyphs,
                        data.page_span_boundaries, data.sha256,
                    )
                    recipe["pymupdf_version"] = fitz.VersionBind
                    recipe["mupdf_version"] = fitz.VersionFitz
                    if text_bbox.get("reused_source_text"):
                        recipe["reused_source_text"] = True
                    if text_bbox.get("partial_source_match"):
                        recipe["partial_source_match"] = True
                        recipe["matched_normalized_characters"] = text_bbox["matched_normalized_characters"]
                        recipe["total_normalized_characters"] = text_bbox["total_normalized_characters"]
                    if text_bbox.get("docling_match"):
                        recipe["docling_match"] = {
                            **text_bbox["docling_match"],
                            "page_index": text_bbox["page_index"], "bbox": text_bbox["bbox"],
                        }
                    if text_bbox.get("ambiguous_match_count"):
                        recipe["ambiguous_match_count"] = text_bbox["ambiguous_match_count"]
                        recipe["match_selection"] = "first_in_page_reading_order"
                    # Equal image.local_text can be restored without storing
                    # additional content. A different value is intentionally
                    # removed and is not recoverable from the text recipe.
                    duplicate_fields = []
                    if isinstance(image_local_text, str) and image_local_text == text:
                        duplicate_fields.append("image.local_text")
                    recipe["removed_duplicate_fields"] = duplicate_fields
                    record["text_reproduction"] = recipe
                    remove_current_text = (remove_text or keep_authorized_text) and not (
                        keep_authorized_text and data.partial_reproduction is True
                    )
                    if remove_current_text:
                        if recipe["literal_alnum_count"] and not allow_content_literals:
                            raise ValueError(
                                f"exact reconstruction needs {recipe['literal_alnum_count']} literal letters or digits; "
                                "text retained (use --allow-content-literals to store them and remove text)"
                            )
                        if recipe["literal_alnum_count"]:
                            print(
                                f"Line {number} ({bulletin_id!r}): warning: recipe retains "
                                f"{recipe['literal_alnum_count']} literal letters or digits",
                                file=sys.stderr,
                            )
                        if text_bbox.get("reused_source_text"):
                            print(
                                f"Line {number} ({bulletin_id!r}): repeated source text is "
                                "preserved by the recipe, not by the bbox alone",
                                file=sys.stderr,
                            )
                        table = record.get("table")
                        if isinstance(table, dict) and table.get("local_text") == text:
                            duplicate_fields.append("table.local_text")
                        if (record.get("pair_type") == "image_caption" and isinstance(image, dict)
                                and image.get("caption") == text):
                            duplicate_fields.append("image.caption")
                        del record["text"]
                        for field in duplicate_fields:
                            container, key = field.split(".")
                            if field != "image.local_text":
                                del record[container][key]
                    matched += 1
                except (ValueError, OSError) as exc:
                    failures += 1
                    print(f"Line {number} ({bulletin_id!r}): {exc}", file=sys.stderr)
                destination.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
        # The input is never replaced. Do not silently overwrite an existing
        # destination, including one created while the input was being read.
        if overwrite:
            os.replace(temp_path, output_path)
        else:
            os.link(temp_path, output_path)
            temp_path.unlink()
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return written, matched, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Hydrated input JSONL")
    parser.add_argument("output", type=Path, help="Output JSONL (must differ from input)")
    parser.add_argument("--pdf-dir", action="append", type=Path, help="PDF root to search recursively; repeatable")
    parser.add_argument("--remove-text", action="store_true", help="Remove matched text and recorded identical duplicates")
    parser.add_argument(
        "--keep-authorized-text", action="store_true",
        help="Remove text except when the PDF mentions partial reproduction; keep recipes in both cases",
    )
    parser.add_argument("--keep-text-bbox", action="store_true", help="Also retain text bounding boxes in the output")
    parser.add_argument("--no-docling", action="store_true", help="Disable the Docling fallback")
    parser.add_argument(
        "--docling-ocr", choices=("easyocr", "tesseract"), default="easyocr",
        help="Docling OCR engine (default: easyocr, matching the construction notebooks)",
    )
    parser.add_argument(
        "--allow-content-literals", action="store_true",
        help="Allow --remove-text when exact reconstruction requires storing literal letters or digits",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()
    try:
        written, matched, failures = process(
            args.input, args.output, args.pdf_dir or DEFAULT_PDF_DIRS,
            remove_text=args.remove_text, overwrite=args.overwrite,
            allow_content_literals=args.allow_content_literals,
            keep_authorized_text=args.keep_authorized_text, keep_text_bbox=args.keep_text_bbox,
            use_docling=not args.no_docling, docling_ocr=args.docling_ocr,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {written} records to {args.output}; matched {matched}; failed {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
