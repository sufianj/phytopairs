import hashlib
import json
import re


def table_shape(html):
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", html or "", flags=re.I | re.S)
    widths = [len(re.findall(r"<(?:td|th)\b", row, flags=re.I)) for row in rows]
    return len(rows), max(widths, default=0)


def canonical_pair(record, split, url_by_id=None):
    pair_type = record.get("pair_type")
    if pair_type not in {"text_image", "text_table", "image_caption"}:
        return None

    asset_kind = "table" if pair_type == "text_table" else "image"
    asset = record.get(asset_kind) or {}
    spans = (asset.get("caption_locators") or []) if pair_type == "image_caption" else (asset.get("local_text_spans") or [])
    if not spans:
        raise ValueError(
            f"Missing text span locators for {record.get('bulletin_id')} / {pair_type}; "
            "rebuild with the release parser before dehydrating."
        )

    if asset_kind == "image":
        bbox = [asset.get("left"), asset.get("top"), asset.get("right"), asset.get("bottom")]
        locator = {"kind": "image", "page": asset.get("page"), "bbox": bbox, "filename": asset.get("filename")}
    else:
        rows, columns = table_shape(asset.get("table_html", ""))
        locator = {"kind": "table", "page": asset.get("page"), "bbox": list(asset.get("bbox") or []), "row_count": rows, "column_count": columns}

    clean_spans = []
    for span in spans:
        page, bbox = span.get("page"), span.get("bbox")
        if page is None or not bbox:
            raise ValueError(f"Malformed text span locator in {record.get('bulletin_id')}")
        clean_spans.append({"page": page, "bbox": list(bbox)})

    identity = {
        "bulletin_id": record.get("bulletin_id"),
        "pair_type": pair_type,
        "asset": locator,
        "text_spans": clean_spans,
    }
    pair_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "pair_id": pair_id,
        "split": split,
        "bulletin_id": record.get("bulletin_id"),
        "pair_type": pair_type,
        "crop": record.get("crop"),
        "date": record.get("date"),
        "year": record.get("year"),
        "region_code": record.get("region_code"),
        "source_url": (url_by_id or {}).get(record.get("bulletin_id")),
        "asset": locator,
        "text_spans": clean_spans,
    }
