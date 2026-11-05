import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import add_bbox_to_jsonl as enrichment


class AddBboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bbox_path = root / "bboxes.jsonl"
        self.input_path = root / "input.jsonl"
        self.output_path = root / "output.jsonl"

    def run_records(self, bbox_records, input_records):
        for path, records in ((self.bbox_path, bbox_records), (self.input_path, input_records)):
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        counts = enrichment.fill_bboxes(self.bbox_path, self.input_path, self.output_path)
        rows = [json.loads(line) for line in self.output_path.read_text(encoding="utf-8").splitlines()]
        return counts, rows

    def test_nested_and_flat_records_copy_page_and_bbox(self):
        counts, rows = self.run_records(
            [
                {"bulletin_id": "a", "pages": [99], "tables": [
                    {"page": 4, "table_html": "nested", "bbox": [1, 2, 3, 4]},
                    {"page": 5, "table_html": "other", "bbox": [5, 6, 7, 8]},
                ]},
                {"bulletin_id": "b", "table_html": "flat", "page": 0, "bbox": [9, 10, 11, 12]},
            ],
            [
                {"bulletin_id": "a", "table_html": "nested", "page": 100},
                {"bulletin_id": "a", "table_html": "other"},
                {"bulletin_id": "b", "table_html": "flat"},
                {"bulletin_id": "missing", "table_html": "nested", "page": 7},
            ],
        )
        self.assertEqual(counts, (3, 1, 0))
        self.assertEqual((rows[0]["page"], rows[0]["bbox"]), (4, [1, 2, 3, 4]))
        self.assertEqual((rows[1]["page"], rows[1]["bbox"]), (5, [5, 6, 7, 8]))
        self.assertEqual((rows[2]["page"], rows[2]["bbox"]), (0, [9, 10, 11, 12]))
        self.assertEqual(rows[3], {"bulletin_id": "missing", "table_html": "nested", "page": 7})

    def test_missing_page_is_not_added_or_overwritten(self):
        counts, rows = self.run_records(
            [{"bulletin_id": "a", "table_html": "flat", "bbox": [1, 2, 3, 4]}],
            [
                {"bulletin_id": "a", "table_html": "flat"},
                {"bulletin_id": "a", "table_html": "flat", "page": 8},
            ],
        )
        self.assertEqual(counts, (2, 0, 0))
        self.assertNotIn("page", rows[0])
        self.assertEqual(rows[1]["page"], 8)

    def test_conflicts_keep_bbox_and_page_from_first_record(self):
        counts, rows = self.run_records(
            [
                {"bulletin_id": "a", "table_html": "same", "page": 4, "bbox": [1, 2, 3, 4]},
                {"bulletin_id": "a", "table_html": "same", "page": 5, "bbox": [1, 2, 3, 4]},
                {"bulletin_id": "a", "table_html": "same", "page": 6, "bbox": [5, 6, 7, 8]},
            ],
            [{"bulletin_id": "a", "table_html": "same"}],
        )
        self.assertEqual(counts, (1, 0, 1))
        self.assertEqual((rows[0]["page"], rows[0]["bbox"]), (4, [1, 2, 3, 4]))


if __name__ == "__main__":
    unittest.main()
