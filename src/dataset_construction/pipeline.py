import io
import json
import os
import random
from collections import Counter
from pathlib import Path
import pymupdf as fitz
import pdfplumber
from PIL import Image as PILImage

import bsv_parser
from bsv_pair_constructor import generate_pairs
from release_manifest import canonical_pair

def loose_extract_images(pdf_path, out_img_dir, bulletin_id, images_raw, text_els=None):
    text_els = text_els or []
    all_text_els_for_page = {}
    for el in text_els:
        all_text_els_for_page.setdefault(el["page"], []).append(el)
    os.makedirs(out_img_dir, exist_ok=True)
    doc, result, seen = fitz.open(pdf_path), [], set()
    for img_info in images_raw:
        xref = img_info["xref"]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            img_data = doc.extract_image(xref)
        except Exception:
            continue
        if len(img_data["image"]) < bsv_parser.MIN_IMAGE_BYTES:
            continue
        if img_data["width"] * img_data["height"] < bsv_parser.MIN_IMAGE_AREA:
            continue

        if img_data["width"] / max(img_data["height"], 1) > 5.5:
            continue  # separator strip or footer banner

        img_area = (img_info.get("right", 0) - img_info.get("left", 0)) * \
                   (img_info.get("bottom", 0) - img_info.get("top", 0))
        if img_area > 0:
            overlap_area = 0
            for el in all_text_els_for_page.get(img_info["page"], []):
                ex0, ey0 = el["left"], el["top"]
                ex1, ey1 = el["left"] + el.get("width", 0), el["top"] + el.get("size", 10)
                ix0 = max(img_info.get("left", 0), ex0)
                iy0 = max(img_info.get("top", 0), ey0)
                ix1 = min(img_info.get("right", 0), ex1)
                iy1 = min(img_info.get("bottom", 0), ey1)
                if ix1 > ix0 and iy1 > iy0:
                    overlap_area += (ix1 - ix0) * (iy1 - iy0)
            if overlap_area / img_area > 0.15:
                continue

        page_w = doc[img_info["page"] - 1].rect.width
        page_h = doc[img_info["page"] - 1].rect.height
        img_w  = img_info.get("right", 0) - img_info.get("left", 0)
        img_h  = img_info.get("bottom", 0) - img_info.get("top", 0)
        if page_w > 0 and page_h > 0:
            coverage = (img_w * img_h) / (page_w * page_h)
            if coverage > 0.40:
                continue

        pil_img = PILImage.open(io.BytesIO(img_data["image"])).convert("RGB")
        pixels = list(pil_img.getdata())
        total = len(pixels)

        near_white = sum(1 for r, g, b in pixels if r > 240 and g > 240 and b > 240)
        if near_white / total > 0.45:
            continue

        near_black = sum(1 for r, g, b in pixels if r < 15 and g < 15 and b < 15)
        if near_black / total > 0.80:
            continue

        from collections import Counter
        quantized = pil_img.quantize(colors=8)
        color_counts = Counter(quantized.getdata())
        if max(color_counts.values()) / total > 0.45:
            continue

        new_name = f"{bulletin_id}_p{img_info['page']}_{xref}.{img_data['ext']}"
        with open(os.path.join(out_img_dir, new_name), "wb") as f:
            f.write(img_data["image"])
        result.append({
            "filename": new_name,
            "page":     img_info["page"],
            "top":      img_info["top"],
            "bottom":   img_info.get("bottom", img_info["top"] + 50),
            "left":     img_info.get("left",   0),
            "right":    img_info.get("right",  1000),
            "caption":  None,
        })
    doc.close()
    return result

_strict_parse_structure = bsv_parser.parse_structure   # save the real one before overwriting

def footer_filtered_parse_structure(pdf_path, table_bboxes=None):
    s_raw, i_raw, text_els = _strict_parse_structure(pdf_path, table_bboxes)

    i_raw = [
        img for img in i_raw
        if not (img.get("page_height") and img["top"] >= 0.90 * img["page_height"])
    ]

    return s_raw, i_raw, text_els

bsv_parser.parse_structure = footer_filtered_parse_structure


def configure_mode(pair_type, split):
    os.environ["SKIP_OCR"] = "1"
    if pair_type == "text_image":
        bsv_parser.MIN_IMAGE_AREA = 40000
        bsv_parser.extract_images = loose_extract_images
        bsv_parser.parse_structure = footer_filtered_parse_structure
        bsv_parser.extract_tables = lambda pdf_path: []
    elif pair_type == "text_table":
        bsv_parser.extract_tables = bsv_parser.docling_extract_tables
        bsv_parser.extract_images = lambda *args, **kwargs: []
    else:
        raise ValueError("pair_type must be text_image or text_table")


def rebuild_pairs(pdf_manifest, pdf_dir, output_dir, split, pair_type, limit=None, output_tag=""):
    configure_mode(pair_type, split)
    output_dir = output_dir / ("Automatic Construction" if split == "automatic" else "Ground Truth")
    if split == "automatic":
        dataset_stem = "auto_textimage" if pair_type == "text_image" else "auto_texttable"
        pair_stem = dataset_stem
        image_dir_name = "bsv_images_auto_textimage" if pair_type == "text_image" else "bsv_images_auto_texttable"
    else:
        dataset_stem = "gt_2404" if pair_type == "text_image" else "gt_tables_realspans"
        pair_stem = "gt_textimage_candidates" if pair_type == "text_image" else "gt_texttable_candidates"
        image_dir_name = "bsv_images_gt_2404" if pair_type == "text_image" else "bsv_images_gt_tables_docling"
    records_path = output_dir / f"bsv_dataset_{dataset_stem}.jsonl"
    pairs_path = output_dir / f"bsv_pairs_{pair_stem}.jsonl"
    if output_tag:
        records_path = records_path.with_name(f"{records_path.stem}_{output_tag}{records_path.suffix}")
        pairs_path = pairs_path.with_name(f"{pairs_path.stem}_{output_tag}{pairs_path.suffix}")
    images_dir = output_dir / image_dir_name
    images_dir.mkdir(parents=True, exist_ok=True)
    rows = [r for r in pdf_manifest if r.get("split") == split]
    if limit is not None:
        rows = rows[:limit]
    url_by_id = {r["bulletin_id"]: r.get("url") for r in rows}
    pair_count = 0
    candidate_pairs = []
    with records_path.open("w", encoding="utf-8") as records_out, pairs_path.open("w", encoding="utf-8") as pairs_out:
        for index, item in enumerate(rows, 1):
            pdf_path = Path(item.get("_local_path", pdf_dir / item["filename"]))
            if not pdf_path.exists():
                raise FileNotFoundError(pdf_path)
            records = bsv_parser.parse_pdf(str(pdf_path), str(images_dir), url=url_by_id.get(item["bulletin_id"]))
            for record in records:
                records_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                pairs = generate_pairs(record)
                for pair in pairs:
                    if pair.get("pair_type") == pair_type or (split == "silver_gt" and pair_type == "text_image" and pair.get("pair_type") == "image_caption"):
                        manifest_record = canonical_pair(pair, split, url_by_id)
                        pair["pair_id"] = manifest_record["pair_id"]
                        pairs_out.write(json.dumps(pair, ensure_ascii=False) + "\n")
                        if split == "silver_gt":
                            candidate_pairs.append(pair)
                        pair_count += 1
            if index % 100 == 0 or index == len(rows):
                print(f"{split}/{pair_type}: {index}/{len(rows)} PDFs, {pair_count} pairs")
    if split == "silver_gt" and not output_tag:
        sample_name = "bsv_pairs_gt_textimage_900.jsonl" if pair_type == "text_image" else "bsv_pairs_gt_texttable_900.jsonl"
        sample_path = output_dir / sample_name
        sample = random.Random(42).sample(candidate_pairs, min(900, len(candidate_pairs)))
        with sample_path.open("w", encoding="utf-8") as f:
            for pair in sample:
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
    return records_path, pairs_path
