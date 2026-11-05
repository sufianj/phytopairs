import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from docling_fallback import DoclingFallback, DoclingFallbackError, document_entries, match_entries, page_box
import locate_jsonl_text as locator
import rehydrate_jsonl_text as rehydrator


def bbox(left, top, right, bottom, origin="TOPLEFT"):
    return SimpleNamespace(l=left, t=top, r=right, b=bottom, coord_origin=origin)


class FakeDocument:
    def __init__(self, items, width=200, height=100):
        self.items = items
        self.texts = []
        self.tables = []
        self.pages = {1: SimpleNamespace(size=SimpleNamespace(width=width, height=height))}

    def iterate_items(self, **kwargs):
        for item in self.items:
            yield item, 0


class DoclingMatchTests(unittest.TestCase):
    def test_provenance_origins_scaling_and_table_cells(self):
        top = bbox(10, 20, 40, 30)
        bottom = bbox(10, 80, 40, 70, "BOTTOMLEFT")
        self.assertEqual(page_box(top, 400, 200, 200, 100), (20, 40, 80, 60))
        self.assertEqual(page_box(bottom, 400, 200, 200, 100), (20, 40, 80, 60))
        self.assertIsNone(page_box(bbox(1, 1, 3, 3, "UNKNOWN"), 200, 100))
        prov = SimpleNamespace(page_no=1, bbox=bottom)
        item = SimpleNamespace(text="Some text", prov=[prov])
        cells = [SimpleNamespace(text="Later", start_row_offset_idx=1, start_col_offset_idx=0),
                 SimpleNamespace(text="Earlier", start_row_offset_idx=0, start_col_offset_idx=0)]
        table = SimpleNamespace(prov=[prov], data=SimpleNamespace(table_cells=cells))
        result = document_entries(FakeDocument([item, table]), [400], [200])
        self.assertEqual(result[0], [((20, 40, 80, 60), "Some text"),
                                     ((20, 40, 80, 60), "Earlier"),
                                     ((20, 40, 80, 60), "Later")])

    def test_multipage_provenance_uses_character_spans(self):
        item = SimpleNamespace(text="ABCDEF", prov=[
            SimpleNamespace(page_no=1, bbox=bbox(1, 1, 20, 10), charspan=(0, 3)),
            SimpleNamespace(page_no=2, bbox=bbox(1, 1, 20, 10), charspan=(3, 6)),
        ])
        result = document_entries(FakeDocument([item]), [200, 200], [100, 100])
        self.assertEqual(result[0][0][1], "ABC")
        self.assertEqual(result[1][0][1], "DEF")

    def test_exact_duplicates_choose_nearest_asset(self):
        text = "The visible passage contains enough words to verify the complete location"
        entries = {0: [((10, 10, 150, 30), text), ((10, 100, 150, 120), text)]}
        result = match_entries(text, entries, locator.normalize, 0, (10, 130, 150, 150))
        self.assertEqual(result["bbox"], [10, 100, 150, 120])
        self.assertEqual(result["docling_match"]["coverage"], 1.0)
        self.assertFalse(result["docling_match"]["approximate"])

    def test_approximate_match_is_scored_and_low_coverage_rejected(self):
        text = "The first sentence contains enough words to anchor this excerpt. A second longer sentence confirms the matching source text."
        recognized = text.replace("confirms", "confirns")
        result = match_entries(text, {0: [((0, 0, 100, 30), recognized)]}, locator.normalize)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["docling_match"]["coverage"], 0.90)
        self.assertLess(result["docling_match"]["coverage"], 1.0)
        self.assertTrue(result["docling_match"]["approximate"])
        self.assertIsNone(match_entries(text, {0: [((0, 0, 100, 30), text[:40])]}, locator.normalize))

    def test_full_query_coverage_with_source_insertions_is_approximate(self):
        text = "The first sentence contains enough words to anchor this excerpt. A second sentence confirms the source text."
        recognized = text.replace("anchor", "anXchor")
        result = match_entries(text, {0: [((0, 0, 100, 30), recognized)]}, locator.normalize)
        self.assertEqual(result["docling_match"]["coverage"], 1.0)
        self.assertTrue(result["docling_match"]["approximate"])

    def test_short_text_requires_complete_normalized_match(self):
        text = "Puceron cendré"
        exact = match_entries(text, {0: [((0, 0, 100, 30), text)]}, locator.normalize)
        self.assertIsNotNone(exact)
        self.assertFalse(exact["docling_match"]["approximate"])
        self.assertIsNone(match_entries(text, {0: [((0, 0, 100, 30), "Puceron cendrX")]}, locator.normalize))

    def test_consecutive_pages_only(self):
        first = "The first page contains enough words to establish this longer example"
        second = "The second page contains a continuation of the same longer example"
        text = first + " " + second
        entries = {0: [((0, 0, 100, 30), first)], 1: [((0, 0, 100, 30), second)]}
        result = match_entries(text, entries, locator.normalize)
        self.assertEqual(result["page_index"], [0, 1])
        self.assertEqual(len(result["bbox"]), 2)
        self.assertIsNone(match_entries(text, {0: entries[0], 2: entries[1]}, locator.normalize))

    def test_more_than_four_consecutive_pages(self):
        parts = [f"Page number {n} has a distinct long sentence about crop observations" for n in range(5)]
        entries = {n: [((0, 0, 100, 30), part)] for n, part in enumerate(parts)}
        result = match_entries(" ".join(parts), entries, locator.normalize)
        self.assertEqual(result["page_index"], [0, 1, 2, 3, 4])

    def test_converter_page_hint_and_cache(self):
        text = "A long and distinctive source paragraph suitable for checking this locator"
        item = SimpleNamespace(text=text, prov=[SimpleNamespace(page_no=1, bbox=bbox(10, 10, 190, 30))])
        class Converter:
            def __init__(self):
                self.calls = []
            def convert(self, path, **kwargs):
                self.calls.append((path, kwargs))
                return SimpleNamespace(document=FakeDocument([item]))
        converter = Converter()
        fallback = DoclingFallback(locator.normalize, converter=converter)
        data = SimpleNamespace(path=Path("example.pdf"), page_widths=[200], page_heights=[100], pages=[None])
        first = fallback.locate(data, text, source_hint=0)
        second = fallback.locate(data, text, source_hint=0)
        self.assertEqual(first, second)
        self.assertEqual(first["docling_match"]["ocr_languages"], ["fr"])
        self.assertTrue(first["docling_match"]["table_structure"])
        self.assertEqual(first["docling_match"]["conversion_page_range"], [1, 1])
        self.assertEqual(converter.calls, [(Path("example.pdf"), {"page_range": (1, 1)})])

    def test_failed_conversion_is_cached(self):
        class Converter:
            def __init__(self):
                self.calls = 0
            def convert(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("model unavailable")
        converter = Converter()
        fallback = DoclingFallback(locator.normalize, converter=converter)
        data = SimpleNamespace(path=Path("example.pdf"), page_widths=[200], page_heights=[100], pages=[None])
        for _ in range(2):
            with self.assertRaisesRegex(DoclingFallbackError, "model unavailable"):
                fallback.convert(data, (1, 1))
        self.assertEqual(converter.calls, 1)


class DoclingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdfs = self.root / "pdfs"
        self.pdfs.mkdir()
        self.input = self.root / "in.jsonl"
        self.output = self.root / "out.jsonl"
        self.restored = self.root / "restored.jsonl"

    def pdf(self, text=None):
        with fitz.open() as document:
            page = document.new_page()
            if text:
                page.insert_text((50, 50), text, fontsize=11)
            document.save(self.pdfs / "a.pdf")

    def write(self, text):
        self.input.write_text(json.dumps({"bulletin_id": "a", "text": text}) + "\n", encoding="utf-8")

    def provider(self):
        class Provider:
            def __init__(self):
                self.calls = 0
            def locate(self, data, text, page, box, source_hint):
                self.calls += 1
                return {"page_index": 0, "bbox": [0, 0, 595, 842],
                        "docling_match": {"coverage": 0.99, "ocr_engine": "test"}}
        return Provider()

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_docling_fallback_preserves_literal_safety_and_roundtrips(self):
        self.pdf("Visible native text")
        text = "Visible native text followed by image text that is unavailable in the PDF text layer"
        self.write(text)
        provider = self.provider()
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.input, self.output, [self.pdfs], remove_text=True, docling_provider=provider)
        self.assertEqual(counts, (1, 0, 1))
        row = self.read(self.output)
        self.assertEqual(row["text"], text)
        self.assertEqual(row["text_reproduction"]["docling_match"]["coverage"], 0.99)
        self.assertGreater(row["text_reproduction"]["literal_alnum_count"], 0)
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.input, self.output, [self.pdfs], remove_text=True,
                                     allow_content_literals=True, overwrite=True, docling_provider=provider)
        self.assertEqual(counts, (1, 1, 0))
        self.assertNotIn("text", self.read(self.output))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rehydrator.process(self.output, self.restored, [self.pdfs]), (1, 0, 0))
        self.assertEqual(self.read(self.restored)["text"], text)

    def test_fully_scanned_document_keeps_authorization_unknown(self):
        self.pdf()
        text = "A scanned passage with enough content to require an OCR based locator"
        self.write(text)
        provider = self.provider()
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.input, self.output, [self.pdfs], keep_authorized_text=True,
                                     docling_provider=provider)
        self.assertEqual(counts, (1, 0, 1))
        row = self.read(self.output)
        self.assertIsNone(row["partial_reproduction"])
        self.assertEqual(row["text"], text)
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.input, self.output, [self.pdfs], remove_text=True,
                                     allow_content_literals=True, overwrite=True, docling_provider=provider)
            self.assertEqual(rehydrator.process(self.output, self.restored, [self.pdfs]), (1, 0, 0))
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(self.read(self.restored)["text"], text)

    def test_docling_not_called_when_pymupdf_matches(self):
        self.pdf("Exact source text")
        self.write("Exact source text")
        provider = self.provider()
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.input, self.output, [self.pdfs], docling_provider=provider)
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(provider.calls, 0)

    def test_docling_error_leaves_text_unchanged(self):
        self.pdf("Visible native text")
        text = "A different passage that is absent from this source PDF"
        self.write(text)
        class BrokenProvider:
            def locate(self, *args):
                raise DoclingFallbackError("model unavailable")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            counts = locator.process(self.input, self.output, [self.pdfs], remove_text=True,
                                     docling_provider=BrokenProvider())
        self.assertEqual(counts, (1, 0, 1))
        self.assertIn("model unavailable", stderr.getvalue())
        self.assertEqual(self.read(self.output)["text"], text)

    def test_no_docling_cli_disables_the_fallback(self):
        self.pdf("Visible native text")
        text = "A different passage that is absent from this source PDF"
        self.write(text)
        script = Path(__file__).resolve().parents[1] / "scripts" / "locate_jsonl_text.py"
        result = subprocess.run(
            [sys.executable, str(script), str(self.input), str(self.output),
             "--pdf-dir", str(self.pdfs), "--remove-text", "--no-docling"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Docling", result.stderr)
        self.assertEqual(self.read(self.output)["text"], text)

    def test_docling_failure_falls_back_to_authorized_partial_trace(self):
        first = "A distinct long opening sentence establishes a reliable source match"
        second = "The second visible sentence adds further source evidence for this example"
        text = first + " " + second + " Missing text from an image is not in the PDF text layer"
        with fitz.open() as document:
            page = document.new_page()
            for i, value in enumerate((first, second, "Reproduction partielle autorisee")):
                page.insert_text((50, 50 + i * 25), value)
            document.save(self.pdfs / "a.pdf")
        self.write(text)
        class BrokenProvider:
            def locate(self, *args):
                raise DoclingFallbackError("model unavailable")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            counts = locator.process(self.input, self.output, [self.pdfs], keep_authorized_text=True,
                                     docling_provider=BrokenProvider())
        self.assertEqual(counts, (1, 1, 0))
        self.assertIn("model unavailable", stderr.getvalue())
        row = self.read(self.output)
        self.assertEqual(row["text"], text)
        self.assertTrue(row["text_reproduction"]["partial_source_match"])
        self.assertNotIn("docling_match", row["text_reproduction"])


if __name__ == "__main__":
    unittest.main()
