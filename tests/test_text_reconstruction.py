import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import locate_jsonl_text as locator
import rehydrate_jsonl_text as rehydrator
from text_reconstruction import build_recipe, restore_recipe


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdfs = self.root / "pdfs"
        self.pdfs.mkdir()
        self.source = self.root / "source.jsonl"
        self.dehydrated = self.root / "dehydrated.jsonl"
        self.restored = self.root / "restored.jsonl"

    def pdf(self, pages, name="a"):
        with fitz.open() as document:
            for lines in pages:
                page = document.new_page()
                for index, text in enumerate(lines):
                    page.insert_text((50, 50 + index * 25), text, fontsize=11)
            document.save(self.pdfs / (name + ".pdf"))

    def write_records(self, records):
        self.source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    def read_records(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def dehydrate(self, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return locator.process(self.source, self.dehydrated, [self.pdfs], remove_text=True, **kwargs)

    def rehydrate(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return rehydrator.process(self.dehydrated, self.restored, [self.pdfs])

    def test_exact_roundtrip_restores_duplicates(self):
        text = "Text to locate"
        self.pdf([[text]])
        originals = [
            {"bulletin_id": "a", "pair_type": "text_image", "text": text, "image": {"local_text": text}},
            {"bulletin_id": "a", "pair_type": "text_table", "text": text, "table": {"local_text": text}},
            {"bulletin_id": "a", "pair_type": "image_caption", "text": text, "image": {"caption": text}},
        ]
        self.write_records(originals)
        self.assertEqual(self.dehydrate(), (3, 3, 0))
        rows = self.read_records(self.dehydrated)
        self.assertTrue(all("text" not in row for row in rows))
        self.assertEqual(rows[0]["text_reproduction"]["pymupdf_version"], fitz.VersionBind)
        self.assertEqual(rows[0]["text_reproduction"]["mupdf_version"], fitz.VersionFitz)
        self.assertTrue(all("text_bbox" not in row for row in rows))
        self.assertNotIn("local_text", rows[0]["image"])
        self.assertNotIn("local_text", rows[1]["table"])
        self.assertNotIn("caption", rows[2]["image"])
        self.assertEqual(self.rehydrate(), (3, 0, 0))
        restored = self.read_records(self.restored)
        for before, after in zip(originals, restored):
            for key, value in before.items():
                self.assertEqual(after[key], value)

    def test_prefers_candidate_with_complete_source_text(self):
        self.pdf([["Doryphores", "Eggs of doryphore hatching now", "doryphores"]])
        text = "Eggs of doryphore hatching now doryphores"
        self.write_records([{
            "bulletin_id": "a", "pair_type": "text_image", "text": text,
            "image": {"page": 1, "left": 50, "top": 20, "right": 250, "bottom": 60},
        }])
        self.assertEqual(self.dehydrate(keep_text_bbox=True), (1, 1, 0))
        record = self.read_records(self.dehydrated)[0]
        self.assertGreater(record["text_bbox"]["bbox"][3], 95)
        self.assertEqual(record["text_reproduction"]["literal_alnum_count"], 0)
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_different_image_local_text_is_removed(self):
        text = "Text to locate"
        self.pdf([[text]])
        self.write_records([{
            "bulletin_id": "a", "pair_type": "text_image", "text": text,
            "image": {"local_text": "Different source text"},
        }])
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.source, self.dehydrated, [self.pdfs], remove_text=True)
        self.assertEqual(counts, (1, 1, 0))
        self.assertNotIn("local_text", self.read_records(self.dehydrated)[0]["image"])
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertNotIn("local_text", self.read_records(self.restored)[0]["image"])
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_multipage_repeated_and_newline_roundtrip(self):
        first = "The first paragraph contains enough words to anchor its beginning"
        last = "The final paragraph contains enough words to anchor its ending"
        self.pdf([[first], [last]])
        text = first + "\n" + last
        self.write_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(self.dehydrate(keep_text_bbox=True), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text_bbox"]["page_index"], [0, 1])
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_repeated_source_text_roundtrips(self):
        passage = "A repeated paragraph contains sufficient text to identify the original source"
        self.pdf([[passage]])
        text = passage + " " + passage
        self.write_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(self.dehydrate(), (1, 1, 0))
        self.assertTrue(self.read_records(self.dehydrated)[0]["text_reproduction"]["reused_source_text"])
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_content_literals_require_explicit_opt_in(self):
        self.pdf([["Some sample text"]])
        text = "SOME sample, text"
        self.write_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(self.dehydrate(), (1, 0, 1))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text"], text)
        self.assertGreater(row["text_reproduction"]["literal_alnum_count"], 0)
        self.assertEqual(self.dehydrate(overwrite=True, allow_content_literals=True), (1, 1, 0))
        self.assertNotIn("text", self.read_records(self.dehydrated)[0])
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_changed_pdf_is_rejected(self):
        self.pdf([["Text to locate"]])
        self.write_records([{"bulletin_id": "a", "text": "Text to locate"}])
        self.assertEqual(self.dehydrate(), (1, 1, 0))
        with (self.pdfs / "a.pdf").open("ab") as pdf:
            pdf.write(b"\n% changed\n")
        self.assertEqual(self.rehydrate(), (0, 0, 1))
        self.assertNotIn("text", self.read_records(self.restored)[0])

    def test_invalid_recipe_cannot_overwrite_existing_data(self):
        self.pdf([["Text to locate"]])
        self.write_records([{"bulletin_id": "a", "text": "Text to locate", "image": {"local_text": "Text to locate"}}])
        self.assertEqual(self.dehydrate(), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        row["image"]["local_text"] = "different"
        self.dehydrated.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(self.rehydrate(), (0, 0, 1))
        result = self.read_records(self.restored)[0]
        self.assertEqual(result["image"]["local_text"], "different")
        self.assertNotIn("text", result)

    def test_text_digest_mismatch_is_rejected(self):
        self.pdf([["Text to locate"]])
        self.write_records([{"bulletin_id": "a", "text": "Text to locate"}])
        self.assertEqual(self.dehydrate(), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        row["text_reproduction"]["text_sha256"] = "0" * 64
        self.dehydrated.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(self.rehydrate(), (0, 0, 1))
        self.assertNotIn("text", self.read_records(self.restored)[0])

    def test_unicode_offsets_and_unmatched_literal(self):
        source = "A🐝ﬁC test"
        glyphs = [(i, i + 1, (i * 5, 0, i * 5 + 4, 10)) for i in range(len(source))]
        text = source + " résumé"
        recipe = build_recipe(
            text, {"page_index": 0, "bbox": [0, 0, 100, 10]}, [source], [glyphs],
            [{0, len(source)}], "0" * 64,
        )
        self.assertGreater(recipe["literal_alnum_count"], 0)
        self.assertEqual(restore_recipe(recipe, [source]), text)

    def test_complete_word_preferred_over_similar_partial_word(self):
        source = "en 2019 xxx en2018."
        glyphs = [(i, i + 1, (i * 5, 0, i * 5 + 4, 10)) for i in range(len(source))]
        text = "en 2018."
        recipe = build_recipe(
            text, {"page_index": 0, "bbox": [0, 0, 200, 10]}, [source], [glyphs],
            [{0, len(source)}], "0" * 64,
        )
        self.assertEqual(recipe["literal_alnum_count"], 0)
        self.assertEqual(restore_recipe(recipe, [source]), text)

    def test_unmatched_original_text_is_preserved(self):
        self.pdf([["Other source text"]])
        self.write_records([{"bulletin_id": "a", "text": "Not in this PDF"}])
        self.assertEqual(self.dehydrate(), (1, 0, 1))
        self.assertEqual(self.rehydrate(), (0, 1, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], "Not in this PDF")

    def test_default_output_removes_image_local_text_and_bbox(self):
        self.pdf([["Text to locate"]])
        self.write_records([{
            "bulletin_id": "a", "text": "Text to locate", "text_bbox": {"stale": True},
            "image": {"local_text": "Different text"},
        }])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(locator.process(self.source, self.dehydrated, [self.pdfs]), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text"], "Text to locate")
        self.assertNotIn("local_text", row["image"])
        self.assertNotIn("text_bbox", row)
        self.assertIn("text_reproduction", row)
        self.assertNotIn("text_reconstruction", row)

    def test_keep_authorized_text_and_remove_other_text(self):
        text = "Text to locate"
        self.pdf([[text, "Reproduction partielle autorisee"]])
        self.pdf([[text]], name="b")
        self.write_records([
            {"bulletin_id": "a", "text": text, "image": {"local_text": text}},
            {"bulletin_id": "b", "text": text, "image": {"local_text": "Different text"}},
            {"bulletin_id": "missing", "text": text, "image": {"local_text": text}},
        ])
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.source, self.dehydrated, [self.pdfs], keep_authorized_text=True)
        self.assertEqual(counts, (3, 2, 1))
        rows = self.read_records(self.dehydrated)
        self.assertTrue(rows[0]["partial_reproduction"])
        self.assertEqual(rows[0]["text"], text)
        self.assertIn("text_reproduction", rows[0])
        self.assertFalse(rows[1]["partial_reproduction"])
        self.assertNotIn("text", rows[1])
        self.assertIn("text_reproduction", rows[1])
        self.assertIsNone(rows[2]["partial_reproduction"])
        self.assertEqual(rows[2]["text"], text)
        self.assertTrue(all("local_text" not in row["image"] for row in rows))
        self.assertEqual(self.rehydrate(), (2, 1, 0))
        restored = self.read_records(self.restored)
        self.assertTrue(all(row["text"] == text for row in restored))
        self.assertEqual(restored[0]["image"]["local_text"], text)
        self.assertNotIn("local_text", restored[1]["image"])

    def test_authorized_text_kept_when_recipe_needs_content_literals(self):
        self.pdf([["Some sample text", "Reproduction partielle autorisee"]])
        text = "SOME sample, text"
        self.write_records([{"bulletin_id": "a", "text": text, "image": {"local_text": text}}])
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.source, self.dehydrated, [self.pdfs], keep_authorized_text=True)
        self.assertEqual(counts, (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text"], text)
        self.assertGreater(row["text_reproduction"]["literal_alnum_count"], 0)
        self.assertNotIn("local_text", row["image"])
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_authorized_partial_source_trace_keeps_text_and_marks_literals(self):
        first = "A distinct long opening sentence establishes a reliable source match"
        second = "The second visible sentence adds further source evidence for this example"
        missing = "An image contains additional words absent from the PDF text layer"
        self.pdf([[first, second, "Reproduction partielle autorisee"]])
        text = first + " " + second + " " + missing
        self.write_records([{"bulletin_id": "a", "text": text}])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            counts = locator.process(self.source, self.dehydrated, [self.pdfs], keep_authorized_text=True)
        self.assertEqual(counts, (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text"], text)
        self.assertTrue(row["text_reproduction"]["partial_source_match"])
        self.assertGreater(row["text_reproduction"]["literal_alnum_count"], 0)
        self.assertIn("only a partial PDF text match", stderr.getvalue())
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_partial_source_trace_requires_permission_to_store_content(self):
        first = "A distinct long opening sentence establishes a reliable source match"
        second = "The second visible sentence adds further source evidence for this example"
        missing = "An image contains additional words absent from the PDF text layer"
        self.pdf([[first, second]])
        text = first + " " + second + " " + missing
        self.write_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(self.dehydrate(), (1, 0, 1))
        self.assertEqual(self.read_records(self.dehydrated)[0]["text"], text)
        self.assertEqual(self.dehydrate(overwrite=True, allow_content_literals=True), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        self.assertNotIn("text", row)
        self.assertTrue(row["text_reproduction"]["partial_source_match"])
        self.assertGreater(row["text_reproduction"]["literal_alnum_count"], 0)
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_remove_text_unless_keep_authorized_option_is_set(self):
        text = "Text to locate"
        self.pdf([[text, "Reproduction partielle autorisee"]])
        self.write_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(self.dehydrate(), (1, 1, 0))
        self.assertNotIn("text", self.read_records(self.dehydrated)[0])
        self.assertEqual(self.dehydrate(overwrite=True, keep_authorized_text=True), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text"], text)
        self.assertTrue(row["partial_reproduction"])
        self.assertIn("text_reproduction", row)

    def test_rehydrate_accepts_legacy_recipe_name(self):
        text = "Text to locate"
        self.pdf([[text]])
        self.write_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(self.dehydrate(), (1, 1, 0))
        row = self.read_records(self.dehydrated)[0]
        row["text_reconstruction"] = row.pop("text_reproduction")
        self.dehydrated.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(self.rehydrate(), (1, 0, 0))
        self.assertEqual(self.read_records(self.restored)[0]["text"], text)

    def test_conflicting_legacy_recipe_is_not_discarded(self):
        text = "Text to locate"
        self.pdf([[text]])
        self.write_records([{
            "bulletin_id": "a", "text": text, "image": {"local_text": "Different"},
            "text_reproduction": {"value": "new"}, "text_reconstruction": {"value": "old"},
        }])
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.source, self.dehydrated, [self.pdfs])
        self.assertEqual(counts, (1, 0, 1))
        row = self.read_records(self.dehydrated)[0]
        self.assertEqual(row["text_reproduction"], {"value": "new"})
        self.assertEqual(row["text_reconstruction"], {"value": "old"})
        self.assertNotIn("local_text", row["image"])


if __name__ == "__main__":
    unittest.main()
