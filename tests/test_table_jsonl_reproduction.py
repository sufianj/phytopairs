import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import table_jsonl_reproduction as codec
import table_cell_reproduction as cells

try:
    import pymupdf as fitz
except ImportError:
    fitz = None


class FakeExtractor:
    engine = "docling"

    def __init__(self, digest, value):
        self.digest, self.value = digest, value

    def get(self, bulletin_id, page):
        return self.digest, [{"page": page, "bbox": (0, 0, 200, 200), "table_html": self.value}]


@unittest.skipIf(fitz is None, "PyMuPDF unavailable")
class TableJsonlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((20, 50), "Apple 10")
        page.insert_text((20, 80), "Cherry > 5")
        page = doc.new_page()
        page.insert_text((20, 50), "Elsewhere")
        doc.save(self.root / "x.pdf")
        doc.close()
        self.cache = cells.PdfCache({"x": self.root / "x.pdf"})
        self.pdf = self.cache.get("x")

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, value):
        return {"pair_type": "text_table", "bulletin_id": "x", "page": 1,
                "bbox": [0, 0, 200, 200], "table_html": value,
                "text_reproduction": {"pdf_sha256": self.pdf.sha256},
                "table_rehydration": {"method": "original", "pdf_sha256": self.pdf.sha256}}

    def test_exact_table_extraction_and_hash_check(self):
        value = "<tr><td>Apple</td></tr>"
        original = self.record(value)
        extractor = FakeExtractor(self.pdf.sha256, value)
        dry, status = codec.dehydrate_record(original, extractor, self.cache)
        self.assertEqual(status, "exact")
        self.assertNotIn("table_html", dry)
        self.assertNotIn(codec.TEMPLATE, dry)
        wet, status = codec.rehydrate_record(dry, extractor, self.cache)
        self.assertEqual(status, "restored")
        self.assertEqual(wet["table_html"], value)
        self.assertEqual(wet["table_rehydration"], original["table_rehydration"])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            codec.rehydrate_record(dry, FakeExtractor(self.pdf.sha256, value + " "), self.cache)

    def test_mismatch_templates_unmatched_cells_and_collision(self):
        value = ('<tr><th colspan="2">Apple</th><td>Missing #literal_0#</td>'
                 '<td>Elsewhere</td><td>Apple</td><td>Cherry &gt; 5</td>'
                 '<td><sup>10</sup></td><td><1</td></tr>')
        original = self.record(value)
        dry, status = codec.dehydrate_record(original, FakeExtractor(self.pdf.sha256, "different"), self.cache)
        self.assertEqual(status, "template")
        self.assertNotIn("table_html", dry)
        self.assertIn("dehydrated_table_html", dry)
        self.assertNotIn("hydrated_table_html", dry)
        template = dry[codec.TEMPLATE]
        self.assertIn('Missing #literal_0#', template)
        self.assertNotIn('>Apple<', template)
        self.assertIn('colspan="2"', template)
        self.assertIn('<sup>', template)
        self.assertIn('<1</td>', template)
        self.assertNotIn('#literal_0#', dry[codec.RECIPE]['literals'])
        self.assertTrue(any(ref.get('page_index') == 1 for ref in dry[codec.RECIPE]['literals'].values()))
        self.assertTrue(any(ref.get('escape') == 'html' for ref in dry[codec.RECIPE]['literals'].values()))
        wet, _ = codec.rehydrate_record(dry, FakeExtractor(self.pdf.sha256, "different"), self.cache)
        self.assertEqual(wet["table_html"], value)
        self.assertEqual(original["table_html"], value)
        dry[codec.TEMPLATE] += 'changed'
        with self.assertRaisesRegex(ValueError, 'template SHA-256'):
            codec.rehydrate_record(dry, FakeExtractor(self.pdf.sha256, "different"), self.cache)

    def test_rehydrate_legacy_template_and_reject_conflicts(self):
        value = '<tr><td>Apple</td><td>Missing</td></tr>'
        extractor = FakeExtractor(self.pdf.sha256, 'different')
        dry, _ = codec.dehydrate_record(self.record(value), extractor, self.cache)
        dry[codec.LEGACY_TEMPLATE] = dry.pop(codec.TEMPLATE)
        wet, _ = codec.rehydrate_record(dry, extractor, self.cache)
        self.assertEqual(wet['table_html'], value)
        dry[codec.TEMPLATE] = dry[codec.LEGACY_TEMPLATE]
        with self.assertRaisesRegex(ValueError, 'conflicting table templates'):
            codec.rehydrate_record(dry, extractor, self.cache)

    def test_truncated_html_and_no_bbox_are_safe(self):
        row = self.record('<tr><td>Apple</td><td>Missing</td><t')
        row.pop('bbox')
        dry, status = codec.dehydrate_record(row, FakeExtractor(self.pdf.sha256, ''), self.cache)
        self.assertEqual(status, 'template_extraction_failed')
        self.assertTrue(dry[codec.TEMPLATE].endswith('<t'))
        wet, _ = codec.rehydrate_record(dry, FakeExtractor(self.pdf.sha256, ''), self.cache)
        self.assertEqual(wet['table_html'], row['table_html'])

    def test_keep_authorized_table_preserves_html_and_verified_recipe(self):
        value = '<tr><td>Apple</td><td>Missing</td></tr>'
        original = self.record(value)
        original['partial_reproduction'] = True
        for extracted, method in ((value, 'table_extraction'), ('different', 'pdf_character_literals')):
            extractor = FakeExtractor(self.pdf.sha256, extracted)
            dry, status = codec.dehydrate_record(original, extractor, self.cache, keep_authorized_table=True)
            self.assertEqual(status, 'kept_authorized')
            self.assertEqual(dry['table_html'], value)
            self.assertEqual(dry[codec.RECIPE]['method'], method)
            wet, status = codec.rehydrate_record(dry, extractor, self.cache)
            self.assertEqual(status, 'restored')
            self.assertEqual(wet['table_html'], value)
            wet['table_html'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'existing table_html'):
                codec.rehydrate_record(wet, extractor, self.cache)

        for permission in (False, None, 'true'):
            row = {**original, 'partial_reproduction': permission}
            dry, _ = codec.dehydrate_record(row, FakeExtractor(self.pdf.sha256, value), self.cache,
                                            keep_authorized_table=True)
            self.assertNotIn('table_html', dry)
        dry, _ = codec.dehydrate_record(original, FakeExtractor(self.pdf.sha256, value), self.cache)
        self.assertNotIn('table_html', dry)

    def test_keep_authorized_cli_option(self):
        value = '<tr><td>Apple</td></tr>'
        row = self.record(value)
        row['partial_reproduction'] = True
        source, output = self.root / 'in.jsonl', self.root / 'out.jsonl'
        source.write_text(json.dumps(row) + '\n')
        counts = codec.process('dehydrate', source, output, [self.root], engine='pymupdf',
                               keep_authorized_table=True)
        self.assertEqual(counts['kept_authorized'], 1)
        dry = json.loads(output.read_text())
        self.assertEqual(dry['table_html'], value)
        self.assertIn(codec.RECIPE, dry)

    def test_nested_table_and_preserve_failed_record(self):
        row = {"pair_type": "text_table", "bulletin_id": "x",
               "table": {"table_html": '<tr><td>Apple</td></tr>'}}
        dry, _ = codec.dehydrate_record(row, FakeExtractor(self.pdf.sha256, ''), self.cache)
        wet, _ = codec.rehydrate_record(dry, FakeExtractor(self.pdf.sha256, ''), self.cache)
        self.assertEqual(wet['table']['table_html'], row['table']['table_html'])
        bad = {"pair_type": "text_table", "bulletin_id": "missing", "table_html": "keep"}
        src, dst = self.root / 'in.jsonl', self.root / 'out.jsonl'
        src.write_text(json.dumps(bad) + '\n')
        counts = codec.process('dehydrate', src, dst, [self.root], engine='pymupdf')
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(json.loads(dst.read_text()), bad)

    def test_native_cli_roundtrip_and_pdf_tampering(self):
        value = '<tr><td>Apple</td><td>Missing</td></tr>'
        src, dry, wet = (self.root / x for x in ('in.jsonl', 'dry.jsonl', 'wet.jsonl'))
        src.write_text(json.dumps(self.record(value)) + '\n')
        counts = codec.process('dehydrate', src, dry, [self.root], engine='pymupdf')
        self.assertEqual(counts['template'] + counts['template_extraction_failed'], 1)
        counts = codec.process('rehydrate', dry, wet, [self.root], engine='pymupdf')
        self.assertEqual(counts['restored'], 1)
        self.assertEqual(json.loads(wet.read_text())['table_html'], value)
        row = json.loads(dry.read_text())
        row[codec.RECIPE]['pdf_sha256'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            codec.rehydrate_record(row, FakeExtractor(self.pdf.sha256, ''), self.cache)

    def test_native_exact_extraction_roundtrip(self):
        path = self.root / 'grid.pdf'
        doc = fitz.open()
        page = doc.new_page()
        for x in (20, 100, 180):
            page.draw_line((x, 30), (x, 100))
        for y in (30, 65, 100):
            page.draw_line((20, y), (180, y))
        for x, y, text in ((30, 50, 'Apple'), (110, 50, '10'),
                           (30, 85, 'Cherry'), (110, 85, '5')):
            page.insert_text((x, y), text)
        doc.save(path)
        doc.close()
        paths = {'grid': path}
        extractor = codec.Extractor(paths, 2, 'pymupdf')
        cache = cells.PdfCache(paths)
        _, candidates = extractor.get('grid', 1)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        row = {'bulletin_id': 'grid', 'pair_type': 'text_table', 'page': 1,
               'bbox': list(candidate['bbox']), 'table_html': candidate['table_html']}
        dry, status = codec.dehydrate_record(row, extractor, cache)
        self.assertEqual(status, 'exact')
        wet, status = codec.rehydrate_record(dry, codec.Extractor(paths, 2, 'pymupdf'), cache)
        self.assertEqual(status, 'restored')
        self.assertEqual(wet['table_html'], row['table_html'])


if __name__ == '__main__':
    unittest.main()
