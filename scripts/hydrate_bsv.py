import argparse
import csv
import hashlib
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def resolve(path):
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_source_dirs(directories):
    sources = []
    for directory in directories:
        source = resolve(directory)
        if not source.is_dir():
            raise NotADirectoryError(source)
        sources.append(source)
    return sources


def choose_local_pdf(filename, directories):
    candidates = [directory / filename for directory in directories if (directory / filename).is_file()]
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    signatures = {(p.stat().st_size, sha256(p)) for p in candidates}
    if len(signatures) > 1:
        raise ValueError(f"Conflicting local copies of {filename}: {candidates}")
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description="Locate or download BSV source PDFs for local use.")
    parser.add_argument("--manifest", default="data/manifests/corpus_manifest.csv")
    parser.add_argument("--output-dir", default="data/local/pdfs")
    parser.add_argument("--source-dir", action="append", default=[], help="Existing local PDF directory; may be repeated.")
    parser.add_argument("--split", choices=("automatic", "silver_gt"))
    parser.add_argument("--start-index", type=int, default=0, help="Start offset within the selected split.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-tag", default="", help="Suffix for shard outputs, e.g. chunk_0001.")
    parser.add_argument("--pin-digests", action="store_true", help="Record digests for rows not yet pinned.")
    parser.add_argument("--rebuild", action="store_true", help="Re-extract local pair data after downloading.")
    parser.add_argument("--pair-type", action="append", choices=("text_image", "text_table"))
    args = parser.parse_args()

    manifest_path = resolve(args.manifest)
    output_dir = resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with manifest_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if args.split:
        rows = [row for row in rows if row.get("split") == args.split]
    if args.start_index < 0:
        parser.error("--start-index must be non-negative")
    if args.output_tag and not all(ch.isalnum() or ch in "_-" for ch in args.output_tag):
        parser.error("--output-tag may contain only letters, digits, underscores, and hyphens")
    rows = rows[args.start_index:]
    if args.limit is not None:
        if args.limit < 0:
            parser.error("--limit must be non-negative")
        rows = rows[:args.limit]

    local_dirs = resolve_source_dirs(args.source_dir)
    local_paths = {}
    updated = False
    for i, row in enumerate(rows, 1):
        filename = Path(row.get("filename") or f"{row['bulletin_id']}.pdf").name
        target = output_dir / filename
        pdf_path = choose_local_pdf(filename, local_dirs)
        downloaded = False
        if pdf_path is None and target.exists():
            pdf_path = target
        if pdf_path is None:
            request = urllib.request.Request(row["url"], headers={"User-Agent": "PHYTO-Pairs data hydrator"})
            with urllib.request.urlopen(request, timeout=90) as response, target.open("wb") as out:
                while block := response.read(1024 * 1024):
                    out.write(block)
            pdf_path = target
            downloaded = True

        size = pdf_path.stat().st_size
        expected = (row.get("sha256") or "").strip()
        expected_size = (row.get("byte_size") or "").strip()
        digest = sha256(pdf_path) if (expected or args.pin_digests) else ""
        if expected and digest.lower() != expected.lower():
            raise ValueError(f"Digest mismatch for {filename}")
        if expected_size and size != int(expected_size):
            raise ValueError(f"Byte-size mismatch for {filename}")
        if not expected:
            if args.pin_digests:
                row["sha256"] = digest
                row["byte_size"] = str(size)
                if downloaded:
                    row["retrieved_at"] = datetime.now(timezone.utc).date().isoformat()
                updated = True
        local_paths[row["bulletin_id"]] = str(pdf_path.resolve())
        if i % 1000 == 0 or i == len(rows):
            print(f"[{i}/{len(rows)}] located {filename}")

    if updated:
        # Update only rows processed by this invocation; retain the rest unchanged.
        key_to_update = {r["bulletin_id"]: r for r in rows}
        with manifest_path.open(encoding="utf-8", newline="") as f:
            all_rows = list(csv.DictReader(f))
            fields = list(all_rows[0]) if all_rows else ["bulletin_id", "filename", "url", "sha256", "byte_size", "retrieved_at", "split"]
        for row in all_rows:
            if row["bulletin_id"] in key_to_update:
                row.update(key_to_update[row["bulletin_id"]])
        with manifest_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"Pinned digests in {manifest_path}")

    if args.rebuild:
        if not args.pair_type:
            parser.error("--rebuild requires at least one --pair-type")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "dataset_construction"))
        from pipeline import rebuild_pairs

        local_manifest = []
        for row in rows:
            local_row = dict(row)
            local_row["_local_path"] = local_paths[row["bulletin_id"]]
            local_manifest.append(local_row)
        derived_root = ROOT / "data/local/source"
        for pair_type in args.pair_type:
            rebuild_pairs(
                local_manifest,
                output_dir,
                derived_root,
                args.split or "automatic",
                pair_type,
                output_tag=args.output_tag,
            )
    print(f"Verified PDFs are in {output_dir}. Derived content remains under ignored data/local/.")


if __name__ == "__main__":
    main()
