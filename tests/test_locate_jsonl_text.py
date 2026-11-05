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


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdfs = self.root / "pdfs"
        self.pdfs.mkdir()
        self.input = self.root / "in.jsonl"
        self.output = self.root / "out.jsonl"

    def pdf(self, name, pages, nested=False):
        directory = self.pdfs / "nested" if nested else self.pdfs
        directory.mkdir(exist_ok=True)
        with fitz.open() as document:
            for lines in pages:
                page = document.new_page()
                for i, text in enumerate(lines):
                    page.insert_text((50, 50 + i * 25), text, fontsize=11)
            document.save(directory / (name + ".pdf"))

    def run_records(self, records, **kwargs):
        self.input.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        kwargs.setdefault("keep_text_bbox", True)
        with contextlib.redirect_stderr(io.StringIO()):
            counts = locator.process(self.input, self.output, [self.pdfs], **kwargs)
        return counts, [json.loads(line) for line in self.output.read_text(encoding="utf-8").splitlines()]

    def test_match_and_reproduction_phrase_with_line_break(self):
        self.pdf("first", [["Some sample text", "Reproduction partielle", "autorisee"]], nested=True)
        self.pdf("second", [["Other text"]])
        counts, rows = self.run_records([
            {"bulletin_id": "first", "text": "SOME sample, text", "index": 7},
            {"bulletin_id": "second", "text": "Other text"},
        ])
        self.assertEqual(counts, (2, 2, 0))
        self.assertEqual(rows[0]["index"], 7)
        self.assertEqual(rows[0]["text"], "SOME sample, text")
        self.assertTrue(rows[0]["partial_reproduction"])
        self.assertFalse(rows[1]["partial_reproduction"])
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)
        self.assertEqual(len(rows[0]["text_bbox"]["bbox"]), 4)
        with fitz.open(self.pdfs / "nested" / "first.pdf") as document:
            extracted = document[0].get_text("text", clip=fitz.Rect(rows[0]["text_bbox"]["bbox"]))
        self.assertEqual(locator.normalize(extracted), locator.normalize(rows[0]["text"]))

    def test_remove_only_matched_and_duplicate_image_text(self):
        self.pdf("a", [["Text to locate"]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": "Text to locate", "image": {"local_text": "Text to locate", "caption": "keep"}},
            {"bulletin_id": "a", "text": "not in source", "image": {"local_text": "not in source"}},
            {"bulletin_id": "missing", "text": "keep me", "partial_reproduction": True},
        ], remove_text=True)
        self.assertEqual(counts, (3, 1, 2))
        self.assertNotIn("text", rows[0])
        self.assertNotIn("local_text", rows[0]["image"])
        self.assertEqual(rows[0]["image"]["caption"], "keep")
        self.assertEqual(rows[1]["text"], "not in source")
        self.assertFalse(rows[1]["partial_reproduction"])
        self.assertIsNone(rows[1]["text_bbox"])
        self.assertIsNone(rows[2]["partial_reproduction"])
        self.assertIsNone(rows[2]["text_bbox"])

    def test_ambiguous_and_image_page_hint(self):
        self.pdf("a", [["Repeated phrase"], ["Repeated phrase"]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": "Repeated phrase"},
            {"bulletin_id": "a", "text": "Repeated phrase", "image": {"page": 2}},
        ])
        self.assertEqual(counts, (2, 2, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)
        self.assertEqual(rows[0]["text_reproduction"]["ambiguous_match_count"], 2)
        self.assertEqual(rows[1]["text_bbox"]["page_index"], 1)

    def test_closest_image_or_table_breaks_same_page_ties(self):
        self.pdf("a", [["Repeated phrase", "Repeated phrase"]])
        image = {"page": 1, "left": 50, "top": 90, "right": 150, "bottom": 110}
        table = {"page": 1, "bbox": [50, 90, 150, 110]}
        counts, rows = self.run_records([
            {"bulletin_id": "a", "pair_type": "text_image", "text": "Repeated phrase", "image": image},
            {"bulletin_id": "a", "pair_type": "text_table", "text": "Repeated phrase", "table": table},
            {"bulletin_id": "a", "pair_type": "text_table", "text": "Repeated phrase", "page": 1, "bbox": table["bbox"]},
            {"bulletin_id": "a", "pair_type": "text_image", "text": "Repeated phrase", "image": {"page": 1}},
        ])
        self.assertEqual(counts, (4, 4, 0))
        for row in rows[:3]:
            self.assertGreater(row["text_bbox"]["bbox"][1], 55)
        self.assertLess(rows[3]["text_bbox"]["bbox"][1], 55)

    def test_reordered_fragments_must_cover_the_whole_text(self):
        first = "The first paragraph contains enough words to anchor its beginning"
        middle = "Caption nearby"
        last = "The final paragraph contains enough words to anchor its ending"
        self.pdf("a", [[first, last, middle]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": first + " " + middle + " " + last},
            {"bulletin_id": "a", "text": first + " missing words " + middle + " " + last},
        ])
        self.assertEqual(counts, (2, 1, 1))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)
        self.assertIsNone(rows[1]["text_bbox"])

    def test_reordered_caption_shorter_than_64_characters(self):
        first = "Aphelinus mali and aphids are present"
        last = "Lanigerum"
        self.pdf("a", [[last, first]])
        counts, rows = self.run_records([{"bulletin_id": "a", "text": first + " " + last}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_reordered_short_final_fragment(self):
        first = "The first paragraph contains enough words to anchor its beginning"
        self.pdf("a", [["FRUIT", first]])
        counts, rows = self.run_records([{"bulletin_id": "a", "text": first + " FRUIT"}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_page_number_in_margin_can_precede_body_text(self):
        body = "The first paragraph contains enough words to anchor its beginning"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((50, 100), body)
            page.insert_text((50, 800), "6")
            document.save(self.pdfs / "a.pdf")
        counts, rows = self.run_records([{"bulletin_id": "a", "text": "6 " + body}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertGreater(rows[0]["text_bbox"]["bbox"][3], 790)

    def test_number_in_body_is_not_used_as_page_number(self):
        body = "The first paragraph contains enough words to anchor its beginning"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((50, 150), "6 unrelated")
            page.insert_text((50, 175), body)
            document.save(self.pdfs / "a.pdf")
        counts, rows = self.run_records([{"bulletin_id": "a", "text": "6 " + body}])
        self.assertEqual(counts, (1, 0, 1))
        self.assertIsNone(rows[0]["text_bbox"])

    def test_leading_number_can_be_part_of_body_text(self):
        first = "2 parcels were observed this week"
        second = "The later observations describe this particular field"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((50, 150), first)
            page.insert_text((50, 175), "Unrelated table material")
            page.insert_text((50, 200), second)
            document.save(self.pdfs / "a.pdf")
        counts, rows = self.run_records([{"bulletin_id": "a", "text": first + " " + second}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_short_intermediate_fragment_can_be_reordered(self):
        first = "A sufficiently long introduction describes the details of this modelling"
        self.pdf("a", [[first, "Poitiers Biard 86", "Niort 79"]])
        text = first + " Niort 79 Poitiers Biard 86"
        counts, rows = self.run_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_long_text_can_end_inside_final_pdf_word(self):
        self.pdf("a", [["The first paragraph contains enough words and ends with irrigated"]])
        text = "The first paragraph contains enough words and ends with irrigate"
        counts, rows = self.run_records([{"bulletin_id": "a", "text": text}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_split_formatting_spans_inside_a_pdf_word(self):
        with fitz.open() as document:
            page = document.new_page()
            x = 50
            for text, font in [("B", "helv"), ("LACK", "hebo"), ("R", "helv"), ("OT", "hebo")]:
                page.insert_text((x, 100), text, fontname=font, fontsize=11)
                x += fitz.get_text_length(text, fontname=font, fontsize=11)
            page.insert_text((x + 5, 100), "The rest of this paragraph has enough text to identify it.")
            document.save(self.pdfs / "a.pdf")
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": "BLACK OT The rest of this paragraph has enough text to identify it."},
        ])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_tied_distances_choose_first(self):
        matches = {(0, (0, 0, 10, 10)), (0, (20, 0, 30, 10))}
        self.assertEqual(locator.nearest_match(matches, 0, (10, 0, 20, 10)), (0, (0, 0, 10, 10)))

    def test_asset_hint_requires_valid_page_and_bbox(self):
        record = {"pair_type": "text_table", "table": {"page": 3, "bbox": [1, 2, 3, 4]}}
        self.assertEqual(locator.asset_hint(record, 2), (None, (1.0, 2.0, 3.0, 4.0)))
        record["table"]["page"] = 1
        record["table"]["bbox"] = [1, 2, float("nan"), 4]
        self.assertEqual(locator.asset_hint(record, 2), (0, None))

    def test_substrings_rejected_and_consecutive_pages_matched(self):
        self.pdf("a", [["Beginning phrase"], ["ending phrase"]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": "ginning"},
            {"bulletin_id": "a", "text": "Beginning phrase ending phrase"},
        ])
        self.assertEqual(counts, (2, 1, 1))
        self.assertIsNone(rows[0]["text_bbox"])
        self.assertEqual(rows[1]["text_bbox"]["page_index"], [0, 1])
        self.assertEqual(len(rows[1]["text_bbox"]["bbox"]), 2)

    def test_multipage_fragments_and_reject_skipped_pages(self):
        first = "The first paragraph contains enough words to anchor its beginning"
        second = "The final paragraph contains enough words to anchor its ending"
        self.pdf("a", [[first], ["Header unrelated to text", second]])
        self.pdf("b", [[first], ["Unrelated intervening page"], [second]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": first + " " + second},
            {"bulletin_id": "b", "text": first + " " + second},
        ])
        self.assertEqual(counts, (2, 1, 1))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], [0, 1])
        self.assertEqual(len(rows[0]["text_bbox"]["bbox"]), 2)
        self.assertIsNone(rows[1]["text_bbox"])

    def test_repeated_source_text_is_marked(self):
        passage = "A repeated paragraph contains sufficient text to identify the original source"
        self.pdf("a", [[passage]])
        counts, rows = self.run_records([{"bulletin_id": "a", "text": passage + " " + passage}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertTrue(rows[0]["text_bbox"]["reused_source_text"])
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_leading_marker_separated_by_heading(self):
        passage = "This paragraph contains enough words to locate a particular piece of text"
        self.pdf("a", [["o Section title", passage]])
        counts, rows = self.run_records([{"bulletin_id": "a", "text": "o " + passage}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_multipage_tie_broken_by_asset_page(self):
        first = "A distinct beginning phrase"
        second = "A distinct ending phrase"
        self.pdf("a", [[first], [second], [first], [second]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": first + " " + second, "image": {"page": 4}},
            {"bulletin_id": "a", "text": first + " " + second},
        ])
        self.assertEqual(counts, (2, 2, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], [2, 3])
        self.assertEqual(rows[1]["text_bbox"]["page_index"], [0, 1])
        self.assertEqual(rows[1]["text_reproduction"]["ambiguous_match_count"], 2)

    def test_ordered_fragments_with_intervening_material(self):
        first = "The first paragraph contains enough words to anchor its beginning"
        second = "The second paragraph contains enough words to anchor its ending"
        self.pdf("a", [[first, "Unrelated table content", second]])
        counts, rows = self.run_records([
            {"bulletin_id": "a", "text": first + " " + second},
            {"bulletin_id": "a", "text": first + " missing words " + second},
        ])
        self.assertEqual(counts, (2, 1, 1))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)
        self.assertIsNone(rows[1]["text_bbox"])

    def test_exact_and_fragmented_duplicates_choose_first(self):
        first = "The first paragraph contains enough words to anchor its beginning"
        second = "The second paragraph contains enough words to anchor its ending"
        self.pdf("a", [[first, second], [first, "Unrelated material", second]])
        counts, rows = self.run_records([{"bulletin_id": "a", "text": first + " " + second}])
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(rows[0]["text_bbox"]["page_index"], 0)

    def test_no_extractable_text_is_unknown(self):
        self.pdf("empty", [[]])
        counts, rows = self.run_records([{"bulletin_id": "empty", "text": "some text"}])
        self.assertEqual(counts, (1, 0, 1))
        self.assertIsNone(rows[0]["partial_reproduction"])

    def test_input_output_safety_and_malformed_json(self):
        self.input.write_text('{"bulletin_id":"x"}\nnot json\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            locator.process(self.input, self.output, [self.pdfs])
        self.assertFalse(self.output.exists())
        with self.assertRaises(ValueError):
            locator.process(self.input, self.input, [self.pdfs])
        self.output.write_text("existing", encoding="utf-8")
        with self.assertRaises(ValueError):
            locator.process(self.input, self.output, [self.pdfs])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing")

    def test_pdf_search_appends_extension_and_searches_nested_directories(self):
        self.pdf("direct", [["Text"]])
        self.pdf("nested", [["Text"]], nested=True)
        found = locator.find_pdfs([self.pdfs], {"direct", "nested"})
        self.assertEqual(found["direct"], self.pdfs / "direct.pdf")
        self.assertEqual(found["nested"], self.pdfs / "nested" / "nested.pdf")

    def test_no_pdf_directories_fails_before_writing_output(self):
        self.input.write_text('{"bulletin_id": "x", "text": "text"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "No PDF directories exist"):
            locator.process(self.input, self.output, [self.root / "missing"])
        self.assertFalse(self.output.exists())

    def test_default_paths_include_local_corpus(self):
        self.assertEqual(locator.DEFAULT_PDF_DIRS, (
            Path(__file__).resolve().parents[1] / "data" / "local" / "pdfs",
        ))

    def test_normalization(self):
        self.assertEqual(locator.normalize("Œuf—Été Æ"), "oeufeteae")


if __name__ == "__main__":
    unittest.main()
