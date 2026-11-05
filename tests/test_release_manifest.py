import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "dataset_construction"))
from release_manifest import canonical_pair


class ReleaseManifestTests(unittest.TestCase):
    def test_image_manifest_omits_source_content_and_has_stable_id(self):
        record = {
            "pair_type": "text_image",
            "bulletin_id": "example",
            "text": "source text",
            "section_title": "source title",
            "image": {
                "filename": "image.jpg", "page": 1,
                "left": 1, "top": 2, "right": 3, "bottom": 4,
                "caption": "source caption",
                "local_text_spans": [{"page": 1, "bbox": [5, 6, 7, 8]}],
            },
        }
        manifest = canonical_pair(record, "train", {"example": "https://example.org/file.pdf"})
        self.assertEqual(manifest["source_url"], "https://example.org/file.pdf")
        self.assertEqual(manifest["text_spans"], [{"page": 1, "bbox": [5, 6, 7, 8]}])
        self.assertNotIn("text", manifest)
        self.assertNotIn("section_title", manifest)
        self.assertNotIn("caption", manifest["asset"])
        self.assertEqual(manifest["pair_id"], canonical_pair(record, "train")["pair_id"])

    def test_table_dimensions_without_html(self):
        record = {
            "pair_type": "text_table", "bulletin_id": "example",
            "table": {
                "page": 1, "bbox": [1, 2, 3, 4],
                "table_html": "<table><tr><th>A</th><th>B</th></tr><tr><td>C</td></tr></table>",
                "local_text_spans": [{"page": 1, "bbox": [5, 6, 7, 8]}],
            },
        }
        asset = canonical_pair(record, "train")["asset"]
        self.assertEqual((asset["row_count"], asset["column_count"]), (2, 2))
        self.assertNotIn("table_html", asset)

    def test_missing_locators_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing text span locators"):
            canonical_pair({"pair_type": "text_image", "bulletin_id": "example", "image": {}}, "train")


if __name__ == "__main__":
    unittest.main()
