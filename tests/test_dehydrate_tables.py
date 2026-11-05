import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dehydrate_tables as dehydrate
import hydrate_tables as hydrate
import table_cell_reproduction as cells
import validate_table_roundtrip as validation

try:
    import pymupdf as fitz
except ImportError:
    fitz = None


class FakeExtractor:
    native_only = False

    def __init__(self, value, sha="abcd"):
        self.value, self.sha = value, sha

    def get(self, bulletin_id):
        return self.sha, [{"page": 1, "bbox": (0, 0, 100, 100), "table_html": self.value}]


class NoPdf:
    def get(self, bulletin_id):
        raise AssertionError("PDF character fallback should not be used")


class DoclingRecipeTests(unittest.TestCase):
    def test_exact_match_and_verified_restoration_preserve_metadata(self):
        value = '<tr><td>Apple &amp; 5</td></tr>'
        original = {"bulletin_id": "x", "pair_type": "text_table", "page": 1,
                    "bbox": [0, 0, 100, 100], "table_html": value,
                    "table_rehydration": {"method": "previous", "pdf_sha256": "abcd"}}
        extractor = FakeExtractor(value)
        dehydrated, method = dehydrate.dehydrate_record(original, extractor, NoPdf(), "docling")
        self.assertEqual(method, "docling")
        self.assertNotIn("table_html", dehydrated)
        self.assertNotIn("Apple", json.dumps(dehydrated))
        self.assertEqual(original["table_html"], value)
        self.assertEqual(hydrate.restore_record(dehydrated, extractor), "restored")
        self.assertEqual(dehydrated["table_html"], value)
        self.assertEqual(dehydrated["table_rehydration"], original["table_rehydration"])

    def test_wrong_html_or_pdf_fails_without_restoration(self):
        value = '<tr><td>Apple</td></tr>'
        original = {"bulletin_id": "x", "pair_type": "text_table", "page": 1,
                    "bbox": [0, 0, 100, 100], "table_html": value}
        dehydrated, _ = dehydrate.dehydrate_record(original, FakeExtractor(value), NoPdf(), "docling")
        for bad in (FakeExtractor('<tr><td>Cherry</td></tr>'), FakeExtractor(value, "other")):
            row = json.loads(json.dumps(dehydrated))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                hydrate.restore_record(row, bad)
            self.assertNotIn("table_html", row)
        with self.assertRaisesRegex(ValueError, "exactly"):
            dehydrate.dehydrate_record(original, FakeExtractor("wrong"), NoPdf(), "docling")


@unittest.skipIf(fitz is None, "PyMuPDF unavailable")
class StrictCellsTests(unittest.TestCase):
    def test_literal_free_for_located_and_unlocated_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((20, 60), 'Apple 10 Cherry > 5 &')
            pdf.save(root / "x.pdf")
            pdf.close()
            value = '<tr><th colspan="2">Apple 10</th></tr>\n<tr><td>Cherry &gt; 5</td><td></td></tr>'
            row = {"bulletin_id": "x", "pair_type": "text_table", "page": 1,
                   "bbox": [1, 2, 300, 200], "table_html": value}
            source, output = root / "in.jsonl", root / "out.jsonl"
            source.write_text(json.dumps(row) + "\n")
            result = dehydrate.process(source, output, [root], method="cells")
            self.assertEqual(result["cells"], 1)
            dehydrated = json.loads(output.read_text())
            self.assertNotIn("table_html", dehydrated)
            recipe = dehydrated["table_cell_reproduction"]
            self.assertEqual(recipe["literal_count"], 0)
            for item in recipe["parts"]:
                for piece in item.get("text", []):
                    self.assertNotIn("literal", piece)
                    self.assertNotIn("accent_case", piece)
            self.assertNotIn("Apple", json.dumps(recipe))
            cache = cells.PdfCache({"x": root / "x.pdf"})
            cells.rehydrate_record(dehydrated, cache)
            self.assertEqual(dehydrated["table_html"], value)
            restored_path = root / "restored.jsonl"
            restored_path.write_text(json.dumps(dehydrated) + "\n")
            self.assertEqual(validation.validate(source, output, restored_path), (1, 0))
            tampered = json.loads(output.read_text())
            tampered["table_cell_reproduction"]["parts"].append({"cell": 0, "text": [{"literal": "secret"}]})
            bad = root / "bad.jsonl"
            bad.write_text(json.dumps(tampered) + "\n")
            self.assertEqual(validation.validate(source, bad, restored_path)[1], 1)

    def test_missing_cell_or_punctuation_never_publishes_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((20, 60), 'Apple')
            pdf.save(root / "x.pdf")
            pdf.close()
            rows = [{"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>Apple</td></tr>"},
                    {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>edited?</td></tr>"}]
            source, output = root / "in.jsonl", root / "out.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.assertRaisesRegex(dehydrate.IncompleteDehydration, "no output"):
                dehydrate.process(source, output, [root], method="cells")
            self.assertFalse(output.exists())
            output.write_text("user data")
            with self.assertRaises(dehydrate.IncompleteDehydration):
                dehydrate.process(source, output, [root], method="cells", overwrite=True)
            self.assertEqual(output.read_text(), "user data")

    def test_record_subset_uses_same_offset_for_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((20, 60), "Apple")
            pdf.save(root / "x.pdf")
            pdf.close()
            rows = [{"bulletin_id": "x", "pair_type": "text_image"},
                    {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>Apple</td></tr>"}]
            source, output, restored = (root / name for name in ("in.jsonl", "out.jsonl", "restored.jsonl"))
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = dehydrate.process(source, output, [root], method="cells", limit=1, start_index=1)
            self.assertEqual(result["cells"], 1)
            row = json.loads(output.read_text())
            cells.rehydrate_record(row, cells.PdfCache({"x": root / "x.pdf"}))
            restored.write_text(json.dumps(row) + "\n")
            self.assertEqual(validation.validate(source, output, restored, limit=1, start_index=1), (1, 0))
            with self.assertRaisesRegex(ValueError, "no input records"):
                dehydrate.process(source, root / "empty.jsonl", [root], method="cells", start_index=99)
            self.assertFalse((root / "empty.jsonl").exists())

    def test_legacy_recipe_is_rebuilt_without_literals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((20, 60), 'Apple 10 > 5')
            pdf.save(root / "x.pdf")
            pdf.close()
            cache = cells.PdfCache({"x": root / "x.pdf"})
            original = {"bulletin_id": "x", "pair_type": "text_table", "table": {
                "table_html": '<tr><td>Apple 10 &gt; 5</td></tr>'}}
            legacy = json.loads(json.dumps(original))
            cells.dehydrate_record(legacy, cache)
            self.assertTrue(any("literal" in piece for item in legacy["table"]["table_cell_reproduction"]["parts"]
                                for piece in item.get("text", [])))
            strict, method = dehydrate.dehydrate_record(legacy, None, cache, "cells")
            self.assertEqual(method, "cells")
            recipe = strict["table"]["table_cell_reproduction"]
            self.assertEqual(recipe["literal_count"], 0)
            validation.verify_cell_recipe(recipe)
            cells.rehydrate_record(strict, cache)
            self.assertEqual(strict["table"]["table_html"], original["table"]["table_html"])

    def test_require_cuda_refuses_cpu_without_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "in.jsonl", root / "out.jsonl"
            source.write_text('{}\n')
            fake_torch = type("Torch", (), {"cuda": type("Cuda", (), {"is_available": staticmethod(lambda: False)})})
            with patch.dict(sys.modules, {"torch": fake_torch}), patch.object(sys, "argv", [
                "dehydrate_tables.py", str(source), str(output), "--require-cuda"
            ]):
                self.assertEqual(dehydrate.main(), 2)
            self.assertFalse(output.exists())

    def test_docling_ocr_cell_references_are_verified_without_literals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "x.pdf"
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((20, 60), "source")
            pdf.save(pdf_path)
            pdf.close()

            class Converter:
                def __init__(self, text):
                    self.text = text

                def convert(self, path):
                    table = Obj(data=Obj(table_cells=[Obj(text=self.text)]))
                    return Obj(status=Obj(value="success"), document=Obj(texts=[], tables=[table]))

            converter = Converter("Apple > 5")
            extractor = Obj(get_converter=lambda: converter)
            cache = cells.PdfCache({"x": pdf_path}, docling_extractor=extractor)
            original = {"bulletin_id": "x", "pair_type": "text_table", "table_html": '<tr><td>Apple &gt; 5</td></tr>'}
            row, method = dehydrate.dehydrate_record(original, None, cache, "cells", ocr_fallback=True)
            self.assertEqual(method, "cells")
            recipe = row["table_cell_reproduction"]
            self.assertIn("docling_text_source", recipe)
            validation.verify_cell_recipe(recipe)
            self.assertNotIn("Apple", json.dumps(recipe))
            self.assertTrue(any("docling_stream" in piece for item in recipe["parts"] for piece in item.get("text", [])))
            fresh_cache = cells.PdfCache({"x": pdf_path}, docling_extractor=extractor)
            cells.rehydrate_record(row, fresh_cache)
            self.assertEqual(row["table_html"], original["table_html"])

            row.pop("table_html")
            converter.text = "Apple > 6"
            fresh_cache = cells.PdfCache({"x": pdf_path}, docling_extractor=extractor)
            with self.assertRaisesRegex(ValueError, "text-stream SHA-256"):
                cells.rehydrate_record(row, fresh_cache)
            self.assertNotIn("table_html", row)


if __name__ == "__main__":
    unittest.main()
