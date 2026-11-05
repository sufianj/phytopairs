import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def resolve(path):
    value = Path(path)
    return value if value.is_absolute() else ROOT / value
sys.path.insert(0, str(ROOT / "src" / "dataset_construction"))
from release_manifest import canonical_pair


def load_pairs(path, origin):
    key_to_pair_ids = defaultdict(set)
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            pair = json.loads(line)
            if pair.get("pair_type") != origin:
                continue
            manifest = canonical_pair(pair, "automatic")
            key = (pair.get("bulletin_id"), pair.get("section_title"), pair.get("text"), origin)
            key_to_pair_ids[key].add(manifest["pair_id"])
    return key_to_pair_ids


def export_one(metadata, reranked_path, pair_ids_by_global, output_path, threshold):
    candidates = pd.read_pickle(reranked_path)
    pairs = set()
    for row in candidates.itertuples(index=False):
        score = getattr(row, "colbert")
        if score < threshold:
            continue
        left = pair_ids_by_global.get(int(getattr(row, "global_i")), set())
        right = pair_ids_by_global.get(int(getattr(row, "global_j")), set())
        for pair_i in left:
            for pair_j in right:
                if pair_i != pair_j:
                    pairs.add((min(pair_i, pair_j), max(pair_i, pair_j), float(score)))
    with Path(output_path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["pair_id_i", "pair_id_j", "similarity"])
        writer.writerows(sorted(pairs))
    return len(pairs)


def main():
    parser = argparse.ArgumentParser(description="Export content-free pair-ID false-negative masks.")
    parser.add_argument("--metadata", default="4.Finetuning/Shared_data/df_combined_metadata.pkl")
    parser.add_argument("--image-similarity", default="4.Finetuning/Shared_data/reranked_image_image.pkl")
    parser.add_argument("--table-similarity", default="4.Finetuning/Shared_data/reranked_table_table.pkl")
    parser.add_argument("--image-pairs", default="3.Dataset Construction/Automatic Construction/bsv_pairs_auto_textimage.jsonl")
    parser.add_argument("--table-pairs", default="3.Dataset Construction/Automatic Construction/bsv_pairs_auto_texttable.jsonl")
    parser.add_argument("--output-dir", default="data/manifests")
    parser.add_argument("--threshold", type=float, default=0.8)
    args = parser.parse_args()

    metadata = pd.read_pickle(resolve(args.metadata))
    image_keys = load_pairs(resolve(args.image_pairs), "text_image")
    table_keys = load_pairs(resolve(args.table_pairs), "text_table")
    maps = {"text_image": defaultdict(set), "text_table": defaultdict(set)}
    for _, row in metadata.iterrows():
        origin = row["origin"]
        if origin not in maps:
            continue
        key = (row["bulletin_id"], row["section_title"], row["text"], origin)
        source = image_keys if origin == "text_image" else table_keys
        for pair_id in source.get(key, ()):
            maps[origin][int(row["_global"])].add(pair_id)

    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    n_image = export_one(metadata, resolve(args.image_similarity), maps["text_image"], output_dir / "negative_masks_text_image.csv", args.threshold)
    n_table = export_one(metadata, resolve(args.table_similarity), maps["text_table"], output_dir / "negative_masks_text_table.csv", args.threshold)
    print(f"Wrote {n_image} image and {n_table} table pair-ID masks to {output_dir}")


if __name__ == "__main__":
    main()
