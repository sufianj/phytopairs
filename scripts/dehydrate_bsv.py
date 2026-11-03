import argparse
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "dataset_construction"))
from release_manifest import canonical_pair

ROOT = Path(__file__).resolve().parents[1]


def resolve(path):
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def url_index(path):
    result = {}
    if not path:
        return result
    with Path(path).open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("bulletin_id") and row.get("url"):
                result[row["bulletin_id"]] = row["url"]
    return result


def main():
    parser = argparse.ArgumentParser(description="Write content-free PHYTO-Pairs manifests.")
    parser.add_argument("--input", action="append", required=True, metavar="SPLIT=JSONL")
    parser.add_argument("--corpus-manifest", default="data/manifests/corpus_manifest.csv")
    parser.add_argument("--output-dir", default="data/manifests")
    args = parser.parse_args()

    urls = url_index(resolve(args.corpus_manifest))
    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped = {}
    for spec in args.input:
        split, sep, filename = spec.partition("=")
        if not sep or not split or not filename:
            parser.error("--input must be SPLIT=JSONL")
        grouped.setdefault(split, []).append(resolve(filename))

    for split, sources in grouped.items():
        out = output_dir / f"pairs_{split}.jsonl"
        written = 0
        with out.open("w", encoding="utf-8") as dst:
            for source in sources:
                with source.open(encoding="utf-8") as src:
                    for line in src:
                        if not line.strip():
                            continue
                        item = canonical_pair(json.loads(line), split, urls)
                        if item is not None:
                            dst.write(json.dumps(item, ensure_ascii=False) + "\n")
                            written += 1
        print(f"{out}: {written} locator records")


if __name__ == "__main__":
    main()
