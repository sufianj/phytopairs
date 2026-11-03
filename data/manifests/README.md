# Release manifests

`corpus_manifest.csv` records `bulletin_id`, filename, source URL, SHA-256, byte size, retrieval date, and split. Hash and size fields can be populated from local PDFs with `scripts/hydrate_bsv.py --source-dir ... --pin-digests`.

The source URL list has 20,215 records but 20,160 unique filenames; 55 additional URLs share a basename with another URL. The canonical manifest retains one URL per filename, so verify that each alternate URL yields the same PDF before treating it as a duplicate. The local PDF folders include two extra filenames suffixed `_1` that have been confirmed byte-identical to already-listed PDFs. They are absent from the canonical manifest because one original-name copy belongs to the held-out split. The legacy automatic text-image JSONL includes two pairs from one such alias; do not treat that legacy file as split-clean without removing the alias and reconciling the reported counts.

Run `scripts/dehydrate_bsv.py` after local extraction and review to generate `pairs_<split>.jsonl`. Pair records retain IDs, split, source URL, modality, asset page/bounding box, table dimensions where applicable, and page/bounding boxes for the text spans. They omit paragraph text, section headings, captions, image pixels, and table content.

No pair manifests are generated in this workspace yet: the existing pair JSONLs lack text-span coordinates. The release parser now captures those locators during local PDF extraction.
