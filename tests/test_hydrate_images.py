import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hydrate_images


class HydrateImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdfs = self.root / "pdfs"
        self.pdfs.mkdir()
        self.pdf = self.pdfs / "test.bulletin.pdf"
        jpeg = io.BytesIO()
        Image.new("RGB", (20, 15), "red").save(jpeg, format="JPEG")
        png = io.BytesIO()
        Image.new("RGB", (12, 10), "blue").save(png, format="PNG")
        with fitz.open() as document:
            page = document.new_page()
            self.jpeg_xref = page.insert_image(fitz.Rect(10, 20, 110, 95), stream=jpeg.getvalue())
            page = document.new_page()
            self.png_xref = page.insert_image(fitz.Rect(10, 20, 110, 100), stream=png.getvalue())
            document.save(self.pdf)
        with fitz.open(self.pdf) as document:
            self.extracted = document.extract_image(self.jpeg_xref)["image"]
            self.png_extracted = document.extract_image(self.png_xref)
        self.digest = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.filename = f"test.bulletin_p1_{self.jpeg_xref}.jpeg"
        self.record = {
            "bulletin_id": "test.bulletin", "pair_type": "text_image",
            "image": {"filename": self.filename, "page": 1, "left": 10, "top": 20, "right": 110, "bottom": 95},
            "text_reproduction": {"pdf_sha256": self.digest},
        }

    def run_hydrate(self, records, *options):
        source = self.root / "input.jsonl"
        source.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        output = self.root / "output.jsonl"
        images = self.root / "images"
        args = ["hydrate_images.py", str(source), "--output", str(output), "--image-dir", str(images), "--pdf-dir", str(self.pdfs), *options]
        with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = hydrate_images.main()
        return code, output, images

    def test_restores_embedded_jpeg_and_png_exactly(self):
        second = dict(self.record, image={"filename": f"test.bulletin_p2_{self.png_xref}.{self.png_extracted['ext']}", "page": 2})
        code, output, images = self.run_hydrate([self.record, second, self.record])
        self.assertEqual(code, 0)
        self.assertEqual((images / self.filename).read_bytes(), self.extracted)
        self.assertEqual((images / second["image"]["filename"]).read_bytes(), self.png_extracted["image"])
        self.assertEqual([json.loads(s) for s in output.read_text().splitlines()], [self.record, second, self.record])

    def test_rejects_wrong_or_missing_pdf_digest(self):
        for recipe in ({"pdf_sha256": "0" * 64}, {}):
            with self.subTest(recipe=recipe):
                record = dict(self.record, text_reproduction=recipe)
                code, _, images = self.run_hydrate([record])
                self.assertEqual(code, 1)
                self.assertFalse((images / self.filename).exists())

    def test_rejects_wrong_page_xref_and_extension(self):
        images = [
            dict(self.record["image"], page=2),
            dict(self.record["image"], filename=f"test.bulletin_p1_{self.png_xref}.png"),
            dict(self.record["image"], filename=f"test.bulletin_p1_{self.jpeg_xref}.png"),
            dict(self.record["image"], filename="../test.bulletin_p1_1.jpeg"),
        ]
        with fitz.open(self.pdf) as document:
            for image in images:
                with self.subTest(image=image), self.assertRaises(ValueError):
                    hydrate_images.embedded_image(document, "test.bulletin", image)

    def test_existing_image_is_not_overwritten_without_permission(self):
        image = self.root / "images" / self.filename
        image.parent.mkdir()
        image.write_bytes(b"existing file")
        code, _, _ = self.run_hydrate([self.record])
        self.assertEqual(code, 1)
        self.assertEqual(image.read_bytes(), b"existing file")
        code, _, _ = self.run_hydrate([self.record], "--overwrite-images")
        self.assertEqual(code, 0)
        self.assertEqual(image.read_bytes(), self.extracted)

    def test_rendered_method_remains_available(self):
        code, _, images = self.run_hydrate([self.record], "--method", "rendered")
        self.assertEqual(code, 0)
        self.assertNotEqual((images / self.filename).read_bytes(), self.extracted)


if __name__ == "__main__":
    unittest.main()
