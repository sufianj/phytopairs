import argparse
import json
import os
import tempfile
from pathlib import Path


def load_bboxes(path: Path) -> tuple[dict[tuple[str, str], dict[str, object]], int]:
    """Index bbox and page values by (bulletin_id, table_html)."""
    bboxes = {}
    conflicts = set()

    def add(record: dict) -> None:
        bulletin_id = record.get("bulletin_id")
        table_html = record.get("table_html")
        bbox = record.get("bbox")
        if not isinstance(bulletin_id, str) or not isinstance(table_html, str) or bbox is None:
            return

        key = (bulletin_id, table_html)
        values = {"bbox": bbox}
        if "page" in record:
            values["page"] = record["page"]
        if key in bboxes and bboxes[key] != values:
            conflicts.add(key)
        else:
            bboxes.setdefault(key, values)

    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path} on line {line_number}: {exc.msg}") from exc
            if not isinstance(record, dict):
                continue

            # Accept either flat table records or bulletin records whose tables
            # (and their bbox and page values) are nested under a "tables" array.
            add(record)
            tables = record.get("tables", [])
            if isinstance(tables, list):
                for table in tables:
                    if isinstance(table, dict):
                        table_record = {**table, "bulletin_id": record.get("bulletin_id")}
                        add(table_record)

    return bboxes, len(conflicts)


def fill_bboxes(bbox_path: Path, input_path: Path, output_path: Path) -> tuple[int, int, int]:
    bboxes, conflicting_keys = load_bboxes(bbox_path)
    joined = 0
    unjoined = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file first so malformed input does not leave a
    # partially-written output behind.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_path.parent, delete=False
        ) as destination:
            temp_path = Path(destination.name)
            with input_path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"invalid JSON in {input_path} on line {line_number}: {exc.msg}"
                        ) from exc
                    if not isinstance(record, dict):
                        raise ValueError(
                            f"expected a JSON object in {input_path} on line {line_number}"
                        )

                    bulletin_id = record.get("bulletin_id")
                    table_html = record.get("table_html")
                    key = (
                        (bulletin_id, table_html)
                        if isinstance(bulletin_id, str) and isinstance(table_html, str)
                        else None
                    )
                    if key is not None and key in bboxes:
                        record.update(bboxes[key])
                        joined += 1
                    else:
                        unjoined += 1

                    destination.write(json.dumps(record, ensure_ascii=False) + "\n")

        os.replace(temp_path, output_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise

    return joined, unjoined, conflicting_keys


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Add bbox and page values to JSONL table records by exact bulletin_id and "
            "table_html match. Unmatched records are kept without modification."
        )
    )
    parser.add_argument("bbox_jsonl", type=Path, help="JSONL containing bbox data")
    parser.add_argument("input_jsonl", type=Path, help="JSONL records to fill")
    parser.add_argument("output_jsonl", type=Path, help="Path for the enriched JSONL")
    args = parser.parse_args()

    paths = [args.bbox_jsonl.resolve(), args.input_jsonl.resolve(), args.output_jsonl.resolve()]
    if len(set(paths)) != len(paths):
        parser.error("bbox input, records input, and output must be three different files")

    try:
        joined, unjoined, conflicts = fill_bboxes(
            args.bbox_jsonl, args.input_jsonl, args.output_jsonl
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Joined lines: {joined}")
    print(f"Unjoined lines: {unjoined}")
    if conflicts:
        print(
            f"Warning: {conflicts} join key(s) had different bbox or page values; "
            "the first values were used for those keys."
        )
    print(f"Wrote {args.output_jsonl}")


if __name__ == "__main__":
    main()
