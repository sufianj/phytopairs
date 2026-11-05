import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import table_cell_reproduction as cells

try:
    import pymupdf as fitz
except ImportError:
    fitz = None


class FakePdf:
    def __init__(self, text):
        self.pages = [text]
        self.sha256 = "1234"
        self.version = "test"
        self.mupdf_version = "test"
        self.boundaries = [set()]
        self.matches = {}
        self.transformed_index = None

    find = cells.PdfCharacters.find
    find_transformed = cells.PdfCharacters.find_transformed


class FakeCache:
    def __init__(self, text):
        self.pdf = FakePdf(text)

    def get(self, bulletin_id):
        return self.pdf


class TableCellTests(unittest.TestCase):
    def test_cli_refuses_legacy_dehydration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "in.jsonl", root / "out.jsonl"
            source.write_text('{}\n')
            result = subprocess.run([sys.executable, str(Path(cells.__file__)), "dehydrate", str(source), str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("dehydrate_tables.py", result.stderr)
            self.assertFalse(output.exists())

    def test_exact_round_trip_keeps_tags_and_removes_words(self):
        original = '<tr><th colspan="2">Apple 10</th></tr>\n<tr><td>Cherry &gt; 5</td><td><sup>2</sup></td></tr>'
        record = {"bulletin_id": "x", "pair_type": "text_table", "table_html": original,
                  "partial_reproduction": False, "text": "unchanged"}
        cache = FakeCache("Apple 10 Cherry 5 2")
        self.assertEqual(cells.dehydrate_record(record, cache), "dehydrated")
        self.assertNotIn("table_html", record)
        recipe = record["table_cell_reproduction"]
        self.assertEqual(recipe["cell_count"], 3)
        self.assertEqual(recipe["literal_alnum_count"], 0)
        stored = json.dumps(recipe, ensure_ascii=False)
        self.assertNotIn("Apple", stored)
        self.assertNotIn("Cherry", stored)
        self.assertIn('colspan', stored)
        self.assertEqual(cells.rehydrate_record(record, cache), "rehydrated")
        self.assertEqual(record["table_html"], original)
        self.assertEqual(record["text"], "unchanged")

    def test_edited_word_preserved_unless_literals_explicitly_allowed(self):
        original = {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>Apple edited</td></tr>"}
        row = dict(original)
        cache = FakeCache("Apple")
        with self.assertRaisesRegex(ValueError, "literal letters"):
            cells.dehydrate_record(row, cache)
        self.assertEqual(row, original)
        cells.dehydrate_record(row, cache, allow_content_literals=True)
        self.assertEqual(row["table_cell_reproduction"]["literal_alnum_count"], 6)
        cells.rehydrate_record(row, cache)
        self.assertEqual(row["table_html"], original["table_html"])

    def test_comparison_and_empty_cells_preserved(self):
        markup = '<tr><td rowspan = "2">F1<30%</td><td></td></tr><tr><td>A &lt; 5</td></tr>'
        segments, count = cells.html_segments(markup)
        self.assertEqual("".join(text for _, text, _ in segments), markup)
        self.assertEqual(count, 3)
        row = {"bulletin_id": "x", "table": {"table_html": markup}}
        cache = FakeCache("F1 30 A 5")
        cells.dehydrate_record(row, cache)
        cells.rehydrate_record(row, cache)
        self.assertEqual(row["table"]["table_html"], markup)

    def test_html_safety_and_malformed_input(self):
        for value in ('<tr><td title="secret">Apple</td></tr>',
                      '<tr><td>Apple</tr>', '<tr><td>&eacute;</td></tr>',
                      '<caption>secret</caption><tr><td>Apple</td></tr>'):
            row = {"bulletin_id": "x", "pair_type": "text_table", "table_html": value}
            with self.assertRaises(ValueError):
                cells.dehydrate_record(row, FakeCache("Apple é"))
            self.assertEqual(row["table_html"], value)

    def test_unmatched_word_cannot_be_invented_from_letters(self):
        row = {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>AB</td></tr>"}
        with self.assertRaisesRegex(ValueError, "literal letters"):
            cells.dehydrate_record(row, FakeCache("A B"))

    def test_truncated_tag_prefix_is_preserved_and_marked(self):
        value = '<tr><td>Apple</td><t'
        row = {"bulletin_id": "x", "pair_type": "text_table", "table_html": value}
        cache = FakeCache("Apple")
        cells.dehydrate_record(row, cache)
        self.assertTrue(row["table_cell_reproduction"]["source_html_truncated"])
        cells.rehydrate_record(row, cache)
        self.assertEqual(row["table_html"], value)

    def test_case_and_accent_edits_reference_complete_source_words(self):
        value = '<tr><td>Vallee</td><td>puceron</td><td>Abbévillois</td></tr>'
        row = {"bulletin_id": "x", "pair_type": "text_table", "table_html": value}
        cache = FakeCache("VALLÉE PUCERON Abbevillois")
        cells.dehydrate_record(row, cache)
        self.assertEqual(row["table_cell_reproduction"]["literal_alnum_count"], 0)
        cells.rehydrate_record(row, cache)
        self.assertEqual(row["table_html"], value)

    def test_digests_and_invalid_reference_fail_safely(self):
        row = {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>Apple</td></tr>"}
        cache = FakeCache("Apple")
        cells.dehydrate_record(row, cache)
        before = json.dumps(row, sort_keys=True)
        cache.pdf.sha256 = "different"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            cells.rehydrate_record(row, cache)
        self.assertEqual(json.dumps(row, sort_keys=True), before)
        cache.pdf.sha256 = "1234"
        recipe = row["table_cell_reproduction"]
        next(part for part in recipe["parts"] if "cell" in part)["text"][0]["end"] = 999
        with self.assertRaisesRegex(ValueError, "out of bounds"):
            cells.rehydrate_record(row, cache)

    def test_only_missing_location_and_partial_option(self):
        cache = FakeCache("Apple")
        row = {"bulletin_id": "x", "pair_type": "text_table", "page": 2,
               "bbox": [1, 2, 3, 4], "table_html": "<tr><td>Apple</td></tr>"}
        self.assertEqual(cells.dehydrate_record(row, cache), "skipped")
        del row["page"], row["bbox"]
        row["partial_reproduction"] = True
        self.assertEqual(cells.dehydrate_record(row, cache, keep_partial=True), "skipped")
        self.assertIn("table_html", row)


@unittest.skipIf(fitz is None, "PyMuPDF unavailable")
class RealPdfTests(unittest.TestCase):
    def test_round_trip_and_unchanged_failure_in_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((20, 60), "Apple 10 Cherry")
            pdf.save(root / "x.pdf")
            pdf.close()
            rows = [{"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>Apple 10</td><td>Cherry</td></tr>"},
                    {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>inventedword</td></tr>"}]
            input_path, dehydrated, restored = (root / p for p in ("in.jsonl", "out.jsonl", "back.jsonl"))
            input_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            result = cells.process("dehydrate", input_path, dehydrated, [root])
            self.assertEqual((result["dehydrated"], result["failed"]), (1, 1))
            data = [json.loads(line) for line in dehydrated.read_text().splitlines()]
            self.assertNotIn("table_html", data[0])
            self.assertEqual(data[1], rows[1])
            result = cells.process("rehydrate", dehydrated, restored, [root])
            self.assertEqual(result["rehydrated"], 1)
            data = [json.loads(line) for line in restored.read_text().splitlines()]
            self.assertEqual(data[0]["table_html"], rows[0]["table_html"])
            self.assertEqual(data[1], rows[1])


if __name__ == "__main__":
    unittest.main()
