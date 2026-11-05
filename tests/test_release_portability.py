import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reserve = load("reserve_gt_pdfs", ROOT / "3.Dataset Construction/Scripts/reserve_gt_pdfs.py")
validator = load("validate_release", ROOT / "scripts/validate_release.py")


class ReleasePortabilityTests(unittest.TestCase):
    def test_reserved_manifest_matches_published_split(self):
        path = ROOT / "3.Dataset Construction/Ground Truth/gt_reserved_pdfs.jsonl"
        with path.open(encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        with (ROOT / "data/manifests/reserved_bulletins.csv").open(encoding="utf-8", newline="") as f:
            reserved = {row["filename"] for row in csv.DictReader(f)}
        self.assertEqual(len(records), 2404)
        self.assertEqual(len({r["filename"] for r in records}), len(records))
        self.assertEqual({r["filename"] for r in records}, reserved)
        self.assertEqual({r["source_dir"] for r in records}, {"data/local/pdfs"})

    def test_generated_repository_path_is_relative(self):
        self.assertEqual(reserve.manifest_directory(ROOT / "data/local/pdfs"), "data/local/pdfs")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(reserve.manifest_directory(directory), directory)

    def test_validator_ignores_checkpoints_but_checks_other_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitignore").write_text("data/local/\nresults/runs/\n.ipynb_checkpoints/\n")
            checkpoint = root / "scripts/.ipynb_checkpoints/ignored.py"
            checkpoint.parent.mkdir(parents=True)
            workstation_path = "/projects" + "/agrifutur/private"
            checkpoint.write_text(workstation_path)
            with patch.object(validator, "ROOT", root), contextlib.redirect_stdout(io.StringIO()):
                validator.main()
            ordinary = root / "scripts/checked.py"
            ordinary.write_text(workstation_path)
            with patch.object(validator, "ROOT", root), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    validator.main()


if __name__ == "__main__":
    unittest.main()
