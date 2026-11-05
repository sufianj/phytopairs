import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hydrate_tables as hydrate
import table_cell_reproduction as cell_reproduction

try:
    import pymupdf as fitz
except ImportError:
    fitz = None


def cell(row, col, text, rowspan=1, colspan=1, header=False):
    return Obj(start_row_offset_idx=row, end_row_offset_idx=row + rowspan,
               start_col_offset_idx=col, end_col_offset_idx=col + colspan,
               row_span=rowspan, col_span=colspan, column_header=header, text=text)


def candidate(page, box, text="<tr><td>example table content</td></tr>"):
    return {"page": page, "bbox": box, "table_html": text}


class FakeExtractor:
    def __init__(self, candidates):
        self.candidates = candidates

    def get(self, bulletin_id):
        return "1234", self.candidates


class HydrateTableTests(unittest.TestCase):
    def test_serialization_matches_construction_parser(self):
        cells = [cell(0, 0, "Title", colspan=2, header=True),
                 cell(1, 0, "left", rowspan=2), cell(1, 1, "right"), cell(2, 1, "fin")]
        table = Obj(data=Obj(table_cells=cells))
        self.assertEqual(hydrate.table_to_html(table),
                         '<tr><th colspan="2">Title</th></tr>'
                         '<tr><td rowspan="2">left</td><td>right</td></tr>'
                         '<tr><td>fin</td></tr>')

    def test_origins_and_scaling(self):
        bottom = Obj(l=10, t=80, r=40, b=70, coord_origin="BOTTOMLEFT")
        top = Obj(l=10, t=20, r=40, b=30, coord_origin="TOPLEFT")
        for box in (bottom, top):
            self.assertEqual(hydrate.page_box(box, 400, 200, 200, 100), (20, 40, 80, 60))

    def test_flat_and_nested_restore_and_preserve_fields(self):
        extractor = FakeExtractor([candidate(3, (10, 20, 110, 120))])
        for nested in (False, True):
            container = {"page": 3, "bbox": [10, 20, 110, 120], "extra": 7}
            record = {"bulletin_id": "abc", "pair_type": "text_table"}
            if nested:
                record["table"] = container
            else:
                record.update(container)
                container = record
            self.assertEqual(hydrate.restore_record(record, extractor), "restored")
            self.assertIn("example table content", container["table_html"])
            self.assertEqual(container["extra"], 7)
            self.assertEqual(record["table_rehydration"]["match_basis"], "bbox_iou")

    def test_missing_locators_only_unique_or_html_match(self):
        first = candidate(1, (0, 0, 10, 10), "<tr><td>first table content</td></tr>")
        second = candidate(2, (0, 0, 10, 10), "<tr><td>other distinctive content</td></tr>")
        record = {"bulletin_id": "x", "pair_type": "text_table", "table_html": second["table_html"]}
        hydrate.restore_record(record, FakeExtractor([first, second]))
        self.assertEqual(record["page"], 2)
        self.assertEqual(record["bbox"], [0, 0, 10, 10])
        missing = {"bulletin_id": "x", "pair_type": "text_table"}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            hydrate.restore_record(missing, FakeExtractor([first, second]))
        self.assertNotIn("table_html", missing)
        hydrate.restore_record(missing, FakeExtractor([first]))
        self.assertEqual(missing["page"], 1)

    def test_missing_page_bbox_ambiguity_and_no_overlap(self):
        first = candidate(1, (0, 0, 10, 10))
        second = candidate(2, (0, 0, 10, 10))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            hydrate.select_table([first, second], None, (0, 0, 10, 10), None)
        with self.assertRaisesRegex(ValueError, "no sufficiently"):
            hydrate.select_table([first], 1, (20, 20, 30, 30), None)
        found, score = hydrate.select_table([first, second], 2, (0, 0, 10, 10), None)
        self.assertEqual(found, second)
        self.assertEqual(score, 1)

    def test_skip_hydrated_check_sha_and_existing_metadata(self):
        first = candidate(1, (0, 0, 10, 10))
        extractor = FakeExtractor([first])
        hydrated = {"pair_type": "text_table", "page": 1, "bbox": [0, 0, 10, 10], "table_html": "original"}
        self.assertEqual(hydrate.restore_record(hydrated, extractor), "skipped")
        record = {"bulletin_id": "x", "pair_type": "text_table", "text_reproduction": {"pdf_sha256": "different"}}
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            hydrate.restore_record(record, extractor)
        record.pop("text_reproduction")
        record["table_rehydration"] = {"existing": True}
        with self.assertRaisesRegex(ValueError, "overwritten"):
            hydrate.restore_record(record, extractor)
        self.assertNotIn("table_html", record)

    def test_comparisons_are_not_treated_as_html_tags(self):
        text = '<tr><td>F1<30% plantes</td><td>< 50</td></tr>'
        self.assertEqual(hydrate.normalized_html(text), 'f130plantes50')

    def test_native_mode_requires_html_and_preserves_it(self):
        extractor = FakeExtractor([candidate(2, (10, 20, 30, 40), 'existing table content')])
        extractor.native_only = True
        record = {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>existing table content</td></tr>"}
        self.assertEqual(hydrate.restore_record(record, extractor), "restored")
        self.assertEqual(record["table_html"], "<tr><td>existing table content</td></tr>")
        self.assertEqual(record["table_rehydration"]["method"], "pymupdf_find_tables")
        self.assertEqual(record["bbox"], [10, 20, 30, 40])
        with self.assertRaisesRegex(ValueError, "requires existing table_html"):
            hydrate.restore_record({"bulletin_id": "x", "pair_type": "text_table"}, extractor)

    def test_process_preserves_failures_and_non_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.pdf").write_bytes(b"pdf")
            source, output = root / "in.jsonl", root / "out.jsonl"
            rows = [{"bulletin_id": "x", "pair_type": "text_table", "page": 1},
                    {"bulletin_id": "x", "pair_type": "text_table", "bbox": [1, 2]},
                    {"bulletin_id": "x", "pair_type": "text_image"}]
            source.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            with patch.object(hydrate, "TableExtractor", return_value=FakeExtractor([candidate(1, (0, 0, 10, 10))])):
                with contextlib.redirect_stderr(io.StringIO()):
                    counts = hydrate.process(source, output, [root])
            self.assertEqual(counts, {"restored": 1, "cells_restored": 0, "skipped": 1, "failed": 1})
            result = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertIn("table_html", result[0])
            self.assertEqual(result[1:], rows[1:])
            with self.assertRaisesRegex(ValueError, "already exists"):
                hydrate.process(source, output, [root])
            with self.assertRaisesRegex(ValueError, "different files"):
                hydrate.process(source, source, [root], overwrite=True)

    def test_invalid_json_does_not_publish_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "in.jsonl", root / "out.jsonl"
            source.write_text('{}\nnot json\n')
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                hydrate.process(source, output, [root])
            self.assertFalse(output.exists())

    def test_extractor_uses_full_pdf_converts_coordinates_and_caches(self):
        class PDF:
            is_encrypted = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __iter__(self):
                return iter([Obj(rect=Obj(width=200, height=100))])

        box = Obj(l=10, t=80, r=40, b=70, coord_origin="BOTTOMLEFT")
        table = Obj(prov=[Obj(page_no=1, bbox=box)], data=Obj(table_cells=[cell(0, 0, "value")]))
        document = Obj(tables=[table], pages={1: Obj(size=Obj(width=200, height=100))})

        class Converter:
            calls = 0

            def convert(self, path):
                self.calls += 1
                return Obj(document=document, status=Obj(value="success"))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.pdf"
            path.write_bytes(b"source")
            extractor = hydrate.TableExtractor({"x": path})
            extractor.converter = Converter()
            with patch.dict(sys.modules, {"pymupdf": Obj(open=lambda path: PDF())}):
                digest, candidates = extractor.get("x")
                self.assertEqual(extractor.get("x"), (digest, candidates))
            self.assertEqual(digest, hydrate.sha256(path))
            self.assertEqual(candidates, [candidate(1, (10, 20, 40, 30), "<tr><td>value</td></tr>")])
            self.assertEqual(extractor.converter.calls, 1)

    def test_pdf_search_respects_root_precedence_and_nested_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "nested").mkdir()
            expected = first / "nested" / "x.PDF"
            expected.write_bytes(b"first")
            (second / "x.pdf").write_bytes(b"second")
            self.assertEqual(hydrate.find_pdfs([first, second], {"x"})["x"], expected)


@unittest.skipIf(fitz is None, "PyMuPDF unavailable")
class AutomaticHydrationTests(unittest.TestCase):
    def test_mixed_records_restore_cells_before_locating_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "x.pdf"
            pdf = fitz.open()
            page = pdf.new_page()
            page.insert_text((30, 60), "Apple 10 Cherry")
            pdf.save(pdf_path)
            pdf.close()
            cache = cell_reproduction.PdfCache({"x": pdf_path})
            flat = {"bulletin_id": "x", "pair_type": "text_table", "table_html": "<tr><td>Apple 10</td></tr>"}
            nested = {"bulletin_id": "x", "pair_type": "text_table", "table": {"table_html": "<tr><td>Cherry</td></tr>"}}
            for row in (flat, nested):
                cell_reproduction.dehydrate_record(row, cache)
            malformed = {"bulletin_id": "x", "pair_type": "text_table", "table_cell_reproduction": {"invalid": True}}
            bad_digest = json.loads(json.dumps(flat))
            bad_digest["table_cell_reproduction"]["pdf_sha256"] = "wrong"
            location = {"bulletin_id": "x", "pair_type": "text_table",
                        "table_html": "<tr><td>example table content</td></tr>"}
            untouched = {"bulletin_id": "x", "pair_type": "text_image"}
            rows = [flat, nested, malformed, bad_digest, location, untouched]
            source, output = root / "in.jsonl", root / "out.jsonl"
            source.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            with patch.object(hydrate, "TableExtractor", return_value=FakeExtractor([candidate(1, (0, 0, 10, 10))])):
                with contextlib.redirect_stderr(io.StringIO()):
                    result = hydrate.process(source, output, [root])
            self.assertEqual(result, {"restored": 1, "cells_restored": 2, "skipped": 1, "failed": 2})
            restored = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(restored[0]["table_html"], "<tr><td>Apple 10</td></tr>")
            self.assertEqual(restored[1]["table"]["table_html"], "<tr><td>Cherry</td></tr>")
            self.assertNotIn("page", restored[0])
            self.assertNotIn("page", restored[1]["table"])
            self.assertEqual(restored[2:4], rows[2:4])
            self.assertEqual(restored[4]["page"], 1)
            self.assertEqual(restored[5], untouched)

            output_only = root / "cells-only.jsonl"
            with patch.object(hydrate, "TableExtractor") as extractor:
                extractor.return_value.get.side_effect = AssertionError("Docling should not run")
                with contextlib.redirect_stderr(io.StringIO()):
                    result = hydrate.process(source, output_only, [root], cells_only=True)
            self.assertEqual(result, {"restored": 0, "cells_restored": 2, "skipped": 2, "failed": 2})
            restored = [json.loads(line) for line in output_only.read_text().splitlines()]
            self.assertEqual(restored[4:], rows[4:])
            extractor.return_value.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
