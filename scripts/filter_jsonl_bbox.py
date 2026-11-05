import argparse
import json
from pathlib import Path


def contains_bbox(value):
    """Return whether a JSON value contains a key named exactly ``bbox``."""
    if isinstance(value, dict):
        return "bbox" in value or any(contains_bbox(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_bbox(item) for item in value)
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Keep JSONL records that contain a bbox key anywhere in the record."
    )
    parser.add_argument("input", type=Path, help="Input JSONL file")
    parser.add_argument("output", type=Path, help="Output JSONL file")
    args = parser.parse_args()

    if args.input.resolve() == args.output.resolve():
        parser.error("input and output must be different files")

    kept = 0
    with args.input.open(encoding="utf-8") as source, args.output.open(
        "w", encoding="utf-8"
    ) as destination:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                parser.error(f"invalid JSON on line {line_number}: {exc.msg}")
            if contains_bbox(record):
                destination.write(line if line.endswith("\n") else line + "\n")
                kept += 1

    print(f"Kept {kept} records in {args.output}")


if __name__ == "__main__":
    main()
