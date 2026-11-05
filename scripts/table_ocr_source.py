"""Deterministic Docling text streams for literal-free table cell references."""

import hashlib
from importlib import metadata
import json

from table_cell_reproduction import PdfCharacters
from text_reconstruction import file_sha256


EXTRACTION = "docling-text-streams-v1"


def stream_digest(streams):
    serialized = json.dumps(streams, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class DoclingTextSource(PdfCharacters):
    """Index Docling text items and table cells without storing their text in recipes."""

    def __init__(self, path, converter):
        self.sha256 = file_sha256(path)
        conversion = converter.convert(path)
        status = getattr(getattr(conversion, "status", None), "value", getattr(conversion, "status", None))
        if status is not None and str(status).lower() != "success":
            raise ValueError(f"Docling conversion was not complete: {status}")
        if file_sha256(path) != self.sha256:
            raise ValueError("source PDF changed during Docling conversion")
        document = conversion.document
        streams = []
        for item in getattr(document, "texts", []):
            value = getattr(item, "text", None)
            if isinstance(value, str) and value:
                streams.append(value)
        for table in getattr(document, "tables", []):
            for cell in getattr(getattr(table, "data", None), "table_cells", []) or []:
                value = getattr(cell, "text", None)
                if isinstance(value, str) and value:
                    streams.append(value)
        if not streams:
            raise ValueError("Docling produced no text streams")
        self.pages = streams
        self.boundaries = [set() for _ in streams]
        self.matches = {}
        self.transformed_index = None
        self.streams_sha256 = stream_digest(streams)
        try:
            self.version = metadata.version("docling")
        except metadata.PackageNotFoundError:
            self.version = "unknown"

    @staticmethod
    def source_ref(reference):
        if reference is None:
            return None
        return {("docling_stream" if key == "page_index" else key): value for key, value in reference.items()}

    def find(self, value):
        return self.source_ref(super().find(value))

    def find_transformed(self, value):
        return self.source_ref(super().find_transformed(value))

    def provenance(self):
        return {"version": 1, "extraction": EXTRACTION, "docling_version": self.version,
                "ocr_engine": "easyocr", "ocr_languages": ["fr"], "streams_sha256": self.streams_sha256}
