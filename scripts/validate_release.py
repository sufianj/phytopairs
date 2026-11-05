import csv
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FIELDS = {"text", "local_text", "caption", "table_html", "rows", "section_title"}
WORKSTATION_PATHS = re.compile("/projects" + "/agrifutur|/mnt" + "/local_agrifutur|/home/" + r"[^/]+/", re.I)


def main():
    errors = []
    warnings = []
    ignore_path = ROOT / ".gitignore"
    ignore_text = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    for required in ("data/local/", "results/runs/"):
        if required not in ignore_text:
            errors.append(f".gitignore must exclude {required}")
    manifest_dir = ROOT / "data" / "manifests"
    for path in manifest_dir.glob("pairs_*.jsonl"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = json.loads(line)
            found = FORBIDDEN_FIELDS.intersection(record)
            asset = record.get("asset", {})
            found.update(FORBIDDEN_FIELDS.intersection(asset))
            if found:
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: forbidden fields {sorted(found)}")
    corpus = manifest_dir / "corpus_manifest.csv"
    if corpus.exists():
        with corpus.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        unpinned = sum(not (row.get("sha256") or "").strip() for row in rows)
        if unpinned:
            warnings.append(f"{unpinned} corpus rows have no pinned SHA-256")
    for path in ROOT.rglob("*"):
        if (not path.is_file() or "data/local" in path.as_posix()
                or ".git" in path.parts or ".ipynb_checkpoints" in path.parts):
            continue
        if path == Path(__file__).resolve():
            continue
        if path.suffix.lower() in {".ipynb", ".py", ".md", ".csv", ".json", ".jsonl", ".txt"}:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if path.suffix.lower() == ".ipynb":
                try:
                    notebook = json.loads(text)
                except json.JSONDecodeError:
                    errors.append(f"{path.relative_to(ROOT)}: invalid notebook JSON")
                    continue
                # Saved output includes historical workstation logs; only notebook source
                # cells can contain active paths that affect reproduction.
                text = "\n".join(
                    "".join(cell.get("source", []))
                    for cell in notebook.get("cells", [])
                    if cell.get("cell_type") in {"code", "markdown"}
                )
            if WORKSTATION_PATHS.search(text):
                errors.append(f"{path.relative_to(ROOT)}: contains a workstation-specific absolute path")
    if errors:
        print("Release validation failed:")
        print("\n".join(f"- {item}" for item in errors))
    else:
        print("No forbidden manifest fields or workstation paths found.")
    for warning in warnings:
        print(f"Warning: {warning}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
