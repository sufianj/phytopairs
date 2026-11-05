"""Locate text with Docling when the PDF text layer is insufficient.

Docling is used only to locate and assess text. The normal reproduction recipe
still references PyMuPDF characters and explicitly stores any needed literals.
"""

from collections import OrderedDict
from difflib import SequenceMatcher
from importlib import metadata
import math


class DoclingFallbackError(ValueError):
    """Docling is unavailable, conversion failed, or no reliable match exists."""


def page_box(bbox, page_width, page_height, source_width=None, source_height=None):
    """Convert Docling provenance coordinates to top-left PDF point coordinates."""
    if bbox is None:
        return None
    try:
        x0, top, x1, bottom = (float(getattr(bbox, key)) for key in ("l", "t", "r", "b"))
        width = float(source_width or page_width)
        height = float(source_height or page_height)
        origin = getattr(bbox.coord_origin, "value", bbox.coord_origin)
        if origin == "BOTTOMLEFT":
            y0, y1 = height - top, height - bottom
        elif origin == "TOPLEFT":
            y0, y1 = top, bottom
        else:
            return None
        box = (x0 * page_width / width, y0 * page_height / height,
               x1 * page_width / width, y1 * page_height / height)
        if not all(math.isfinite(value) for value in box):
            return None
        x0, y0, x1, y1 = box
        x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(page_width, x1), min(page_height, y1)
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None
    except (AttributeError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def document_entries(document, page_widths, page_heights):
    """Read text and table-cell items with validated page provenance."""
    try:
        iterator = document.iterate_items(traverse_pictures=True)
    except TypeError:
        iterator = document.iterate_items()
    items = []
    seen = set()
    for item, _ in iterator:
        items.append(item)
        seen.add(id(item))
    # Some versions exclude headers, footers or unlinked text from iteration.
    for item in list(getattr(document, "texts", [])) + list(getattr(document, "tables", [])):
        if id(item) not in seen:
            items.append(item)
            seen.add(id(item))
    result = {}
    pages = getattr(document, "pages", {})
    for item in items:
        provenance = list(getattr(item, "prov", []) or [])
        if not provenance:
            continue
        text = getattr(item, "text", None)
        cells = getattr(getattr(item, "data", None), "table_cells", None)
        if not isinstance(text, str) and not cells:
            continue
        for prov in provenance:
            page_number = getattr(prov, "page_no", None)
            if isinstance(page_number, bool) or not isinstance(page_number, int):
                continue
            page_index = page_number - 1
            if not 0 <= page_index < len(page_widths):
                continue
            page_item = pages.get(page_number) if hasattr(pages, "get") else None
            size = getattr(page_item, "size", None)
            box = page_box(
                getattr(prov, "bbox", None), page_widths[page_index], page_heights[page_index],
                getattr(size, "width", None), getattr(size, "height", None),
            )
            if box is None:
                continue
            if isinstance(text, str) and text.strip():
                # Multi-page text has per-page character spans. Do not attach
                # the complete text to every page.
                part = text
                if len(provenance) > 1:
                    span = getattr(prov, "charspan", None)
                    if not isinstance(span, (tuple, list)) or len(span) != 2:
                        continue
                    try:
                        first, last = span
                        if not (isinstance(first, int) and isinstance(last, int) and 0 <= first < last <= len(text)):
                            continue
                        part = text[first:last]
                    except (TypeError, ValueError):
                        continue
                result.setdefault(page_index, []).append((box, part))
            elif cells and len(provenance) == 1:
                # The notebook uses the table provenance bbox. Cell bboxes
                # differ by Docling backend, so keep the validated table box.
                ordered = sorted(cells, key=lambda cell: (
                    getattr(cell, "start_row_offset_idx", 0), getattr(cell, "start_col_offset_idx", 0),
                ))
                for cell in ordered:
                    cell_text = getattr(cell, "text", None)
                    if isinstance(cell_text, str) and cell_text.strip():
                        result.setdefault(page_index, []).append((box, cell_text))
    return result


def match_entries(text, entries, normalize, asset_page=None, asset_box=None):
    """Find a high-coverage normalized match in one or consecutive pages."""
    query = normalize(text)
    if len(query) < 8 or not entries:
        return None
    page_ids = sorted(entries)
    windows = [(page,) for page in page_ids]
    for count in range(2, min(4, len(page_ids)) + 1):
        for start in range(len(page_ids) - count + 1):
            window = tuple(page_ids[start:start + count])
            if window == tuple(range(window[0], window[-1] + 1)):
                windows.append(window)
    if len(page_ids) > 4 and page_ids == list(range(page_ids[0], page_ids[-1] + 1)):
        # Also allow longer contiguous excerpts without testing every possible
        # O(page_count²) window. Matched pages are validated separately below.
        windows.append(tuple(page_ids))
    candidates = {}
    for window in windows:
        for spatial_order in (False, True):
            flattened = []
            for page in window:
                rows = entries[page]
                if spatial_order:
                    rows = sorted(rows, key=lambda item: (item[0][1], item[0][0], item[0][3], item[0][2]))
                flattened.extend((page, bbox, normalize(part)) for bbox, part in rows if normalize(part))
            if not flattened:
                continue
            stream = "".join(part for _, _, part in flattened)
            indexes = [index for index, (_, _, part) in enumerate(flattened) for _ in part]

            def add_match(ranges, matched, exact):
                selected = {}
                for start, end in ranges:
                    for index in set(indexes[start:end]):
                        page, bbox, _ = flattened[index]
                        selected.setdefault(page, set()).add(bbox)
                pages = tuple(sorted(selected))
                if not pages or pages != tuple(range(pages[0], pages[-1] + 1)):
                    return
                boxes = tuple((min(b[0] for b in selected[p]), min(b[1] for b in selected[p]),
                               max(b[2] for b in selected[p]), max(b[3] for b in selected[p])) for p in pages)
                key = (pages, boxes)
                candidates[key] = max(candidates.get(key, (0, False)), (matched, exact))

            # Preserve all exact occurrences so the asset location can select
            # the nearest rather than relying on SequenceMatcher's first hit.
            pos = stream.find(query)
            while pos != -1:
                add_match(((pos, pos + len(query)),), len(query), True)
                pos = stream.find(query, pos + 1)
            if len(query) < 32:
                # Short passages are too easy to align spuriously; only a
                # complete normalized occurrence is accepted.
                continue
            blocks = [block for block in SequenceMatcher(None, query, stream, autojunk=False).get_matching_blocks()
                      if block.size >= 4]
            matched = sum(block.size for block in blocks)
            longest = max((block.size for block in blocks), default=0)
            if matched / len(query) >= 0.90 and longest >= min(64, max(24, len(query) // 5)):
                add_match(((block.b, block.b + block.size) for block in blocks), matched,
                          len(blocks) == 1 and matched == len(query))
    if not candidates:
        return None
    if asset_page is not None:
        distance = min(min(abs(page - asset_page) for page in pages) for pages, _ in candidates)
        candidates = {key: value for key, value in candidates.items()
                      if min(abs(page - asset_page) for page in key[0]) == distance}
    best = max(candidates.values())
    choices = [key for key, value in candidates.items() if value == best]
    if asset_box is not None and asset_page is not None and all(asset_page in pages for pages, _ in choices):
        ax0, ay0, ax1, ay1 = asset_box
        def distance(key):
            pages, boxes = key
            x0, y0, x1, y1 = boxes[pages.index(asset_page)]
            dx, dy = max(ax0 - x1, x0 - ax1, 0), max(ay0 - y1, y0 - ay1, 0)
            cx, cy = (x0 + x1 - ax0 - ax1) / 2, (y0 + y1 - ay0 - ay1) / 2
            return (dx * dx + dy * dy, cx * cx + cy * cy)
        closest = min(distance(key) for key in choices)
        choices = [key for key in choices if distance(key) == closest]
    pages, boxes = min(choices, key=lambda key: (key[0][0], key[1][0][1], key[1][0][0], key[0], key[1]))
    result = {
        "page_index": pages[0] if len(pages) == 1 else list(pages),
        "bbox": list(boxes[0]) if len(pages) == 1 else [list(box) for box in boxes],
        "docling_match": {
            "matched_normalized_characters": best[0],
            "total_normalized_characters": len(query),
            "coverage": round(best[0] / len(query), 6),
            "approximate": not best[1],
        },
    }
    if len(choices) > 1:
        result["ambiguous_match_count"] = len(choices)
    return result


class DoclingFallback:
    """Lazily load one converter and cache converted PDF page ranges."""

    def __init__(self, normalize, engine="easyocr", converter=None, cache_size=8):
        self.normalize = normalize
        self.engine = engine
        self.converter = converter
        self.cache_size = cache_size
        self.cache = OrderedDict()
        self.init_error = None

    def get_converter(self):
        if self.init_error is not None:
            raise DoclingFallbackError(self.init_error)
        if self.converter is None:
            try:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption
                options = PdfPipelineOptions()
                options.do_ocr = True
                options.do_table_structure = True
                if self.engine == "easyocr":
                    from docling.datamodel.pipeline_options import EasyOcrOptions
                    options.ocr_options = EasyOcrOptions(lang=["fr"])
                else:
                    from docling.datamodel.pipeline_options import TesseractCliOcrOptions
                    options.ocr_options = TesseractCliOcrOptions(lang=["fra"])
                self.converter = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                })
            except Exception as exc:
                self.init_error = f"Docling initialization failed: {exc}"
                raise DoclingFallbackError(self.init_error) from exc
        return self.converter

    def convert(self, data, page_range):
        key = (str(data.path), page_range)
        if key not in self.cache:
            try:
                converter = self.get_converter()
                result = converter.convert(data.path, page_range=page_range) if page_range else converter.convert(data.path)
                entries = document_entries(result.document, data.page_widths, data.page_heights)
                self.cache[key] = entries
            except Exception as exc:
                self.cache[key] = DoclingFallbackError(f"Docling conversion failed: {exc}")
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        result = self.cache[key]
        if isinstance(result, Exception):
            raise result
        return result

    def locate(self, data, text, asset_page=None, asset_box=None, source_hint=None):
        hint = asset_page if asset_page is not None else source_hint
        ranges = []
        if hint is not None and 0 <= hint < len(data.pages):
            ranges.append((hint + 1, hint + 1))  # Docling page_range is 1-based and inclusive.
            nearby = (max(1, hint), min(len(data.pages), hint + 2))
            if nearby != ranges[0]:
                ranges.append(nearby)
        ranges.append(None)
        for page_range in ranges:
            entries = self.convert(data, page_range)
            match = match_entries(text, entries, self.normalize, asset_page, asset_box)
            if match is not None:
                try:
                    version = metadata.version("docling")
                except metadata.PackageNotFoundError:
                    version = "unknown"
                match["docling_match"].update({
                    "docling_version": version,
                    "ocr_engine": self.engine,
                    "ocr_languages": ["fr"] if self.engine == "easyocr" else ["fra"],
                    "table_structure": True,
                    "conversion_page_range": list(page_range) if page_range else None,
                })
                return match
        raise DoclingFallbackError("Docling did not find a sufficiently complete text match")
