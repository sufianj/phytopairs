"""
reserve_gt_pdfs.py

Randomly reserves a fixed set of PDFs for GT construction, drawn from BOTH
source folders combined, and writes a manifest listing which ones (filename
+ source folder). This manifest is the reservation record, used later to:
  - build the GT-specific (loosened-settings) batch input list
  - exclude these exact filenames from the automatic 20k-corpus construction
    run, per the "never reuse GT PDFs for auto-construction" rule

This script does NOT copy, move, rename, or write anything into the shared
PDF folders. It only reads filenames from them and writes the manifest into
your own project folder.

Usage:
    python3 reserve_gt_pdfs.py
"""

import os
import random
import json

DIR_A = "/projects/agrifutur/bsv_pdfs"        # ~1000 PDFs
DIR_B = "/projects/agrifutur/bsv_pdfs_all"    # the rest, no overlap with DIR_A
N_RESERVE = 3205
SEED = 42  # fixed seed so this exact selection is reproducible if regenerated
OUT_MANIFEST = "/projects/agrifutur/yabusaqe/BSV Sample Construction/Ground Truth/gt_reserved_pdfs.jsonl"


def list_pdfs(directory):
    return sorted(f for f in os.listdir(directory) if f.endswith(".pdf"))


def main():
    files_a = list_pdfs(DIR_A)
    files_b = list_pdfs(DIR_B)

    print(f"{DIR_A}: {len(files_a)} PDFs")
    print(f"{DIR_B}: {len(files_b)} PDFs")

    # Defensive check, not assumed: confirm there really is no filename
    # overlap between the two folders before treating them as one pool
    names_a = set(files_a)
    names_b = set(files_b)
    overlap = names_a & names_b
    if overlap:
        print(f"WARNING: {len(overlap)} filenames appear in BOTH directories, "
              f"first few: {list(overlap)[:5]}")
        print("Stopping, since this contradicts the 'nothing is duplicated' assumption.")
        return
    print("Confirmed: no overlapping filenames between the two directories.")

    combined = [(f, DIR_A) for f in files_a] + [(f, DIR_B) for f in files_b]
    total = len(combined)
    print(f"Combined pool: {total} PDFs")

    if N_RESERVE > total:
        raise ValueError(f"Requested {N_RESERVE} PDFs but only {total} exist in total.")

    random.seed(SEED)
    shuffled = combined[:]
    random.shuffle(shuffled)
    reserved = shuffled[:N_RESERVE]

    from_a = sum(1 for _, d in reserved if d == DIR_A)
    from_b = sum(1 for _, d in reserved if d == DIR_B)
    print(f"Reserved {len(reserved)} PDFs for GT: {from_a} from {DIR_A}, {from_b} from {DIR_B}")

    os.makedirs(os.path.dirname(OUT_MANIFEST), exist_ok=True)
    with open(OUT_MANIFEST, "w", encoding="utf-8") as f:
        for fname, directory in reserved:
            f.write(json.dumps({"filename": fname, "source_dir": directory}) + "\n")

    print(f"\nManifest written to: {OUT_MANIFEST}")
    print("This manifest is the reservation record only, no PDF was moved or copied.")
    print("Use it to (a) build the GT batch's input list, and (b) exclude these exact "
          "filenames from the automatic 20k-corpus construction run.")


if __name__ == "__main__":
    main()
