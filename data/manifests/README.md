# Release manifests

`corpus_manifest.csv` records each bulletin's ID, PDF filename, source URL, and split. `reserved_bulletins.csv` and `reserved_bulletins.jsonl` identify the held-out PDF pool; `bulletin_metadata.csv` contains bulletin dates and regions. `leaked_text_image_pairs.csv` identifies flagged legacy text–image pairs.

The legacy `3.Dataset Construction/Ground Truth/gt_reserved_pdfs.jsonl` lists the same reserved filenames; its `source_dir` is the repository-relative PDF location `data/local/pdfs`, not the original workstation folder.

The [PDF URL list](pdf_urls.txt) has 20,215 entries and 20,160 distinct filenames. The manifest retains one URL per filename; the contents of URLs sharing a filename have not been reconciled. Two additional `_1` files in the original local corpus duplicate listed PDFs. A legacy training alias refers to a PDF whose original filename is in the held-out split, so the legacy pairs are not strictly document-disjoint. The list was obtained by querying the [PHB Knowledge Graph](https://rdf.codex.cati.inrae.fr/bsv-res/snorql/?query=%0D%0ASELECT+distinct+%3Fbsvurl+WHERE+%7B%0D%0A+%3Fb+a+bsvo%3ABulletin%3B%0D%0A++++dul%3AisRealizedBy+%3FdigitalDoc.%0D%0A%3FdigitalDoc+schema%3AisBasedOn+%3Fbsvsrc%3B%0D%0Aschema%3Aurl+%3Fbsvurl.%0D%0A%7D) on 14 June 2026.


The [dehydrated ground-truth sets](../Dehydrated%20Data/Ground%20Truth/) and [reproduction workflow](../../README.md#data-reproduction-workflow) are described in the repository README. The experiment notebooks expect rehydrated text–image and text–table ground truth as `3.Dataset Construction/Ground Truth/bsv_gt_textimage_final_500.jsonl` and `3.Dataset Construction/Ground Truth/bsv_gt_texttable_final_merged.jsonl`, respectively, and images in `3.Dataset Construction/Ground Truth/bsv_images_gt_2404/`.

The construction notebooks write generated pairs and image crops locally. Source PDFs and extracted content remain subject to their publishers' terms.
