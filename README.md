# PHYTO-Pairs Reproduction Package

Code and data for the paper *PhytoPairs: A French Multimodal Retrieval Dataset and Toolkit for Agricultural Texts, Photographs, and Tables*.

The French Plant Health Bulletins (Bulletins de Santé du Végétal, BSV) are generally expected to be available under the [Licence Ouverte (Etalab)](https://github.com/etalab/licence-ouverte/blob/master/LO.md), but its applicability to individual bulletins and third-party content has not been verified. Some ground-truth text is retained where a bulletin explicitly states that partial reproduction is authorized. Table templates may also retain source content; ground-truth images are dehydrated.

## Repository map

| Paper section | Contents |
|---|---|
| 3 — Dataset | Parser, pair construction, batch notebooks, curation code, manifests |
| 4 — Cross-modal IR | Zero-shot baselines, layer sweeps, data-size runs, metadata-mining runs |
| 5 — Citrus case study | Citrus benchmark preparation, evaluation, and analysis |
| `data/manifests/` | Source URLs, corpus and held-out metadata; see its [README](data/manifests/README.md) |
| `data/external/bbch_citrus/` | Acquisition notes for external citrus inputs |
| `results/` | Compact Section 4 and citrus reference metrics |

## Data reproduction workflow

1. **Download the PDFs** listed in [PDF_URLS.txt](3.Dataset%20Construction/PDFs%20Download/PDF_URLS.txt), using the [download notebook](3.Dataset%20Construction/PDFs%20Download/bsv_download.ipynb). Place them in `data/local/pdfs/`.
2. **Rehydrate the ground truth:** use the [text](scripts/rehydrate_jsonl_text.py), [table](scripts/table_jsonl_reproduction.py), and [image](scripts/hydrate_images.py) rehydration scripts for the [text–image](data/Dehydrated%20Data/Ground%20Truth/bsv_gt_textimage_final_500_dehydrated.jsonl) and [text–table](data/Dehydrated%20Data/Ground%20Truth/bsv_gt_texttable_final_500_dehydrated.jsonl) sets. Chain the outputs for each modality and place the resulting files where the experiment notebooks expect them, as described in the [manifest README](data/manifests/README.md). Some text and table content can be previewed without downloading the PDFs. Text is retained where the bulletin states that partial reproduction is authorized; partially dehydrated table templates can retain content regardless of that flag. Ground-truth images are dehydrated.

   From the repository root, for example:

   ```sh
   python scripts/rehydrate_jsonl_text.py 'data/Dehydrated Data/Ground Truth/bsv_gt_textimage_final_500_dehydrated.jsonl' data/local/gt_image_text.jsonl --pdf-dir data/local/pdfs
   python scripts/hydrate_images.py data/local/gt_image_text.jsonl --output '3.Dataset Construction/Ground Truth/bsv_gt_textimage_final_500.jsonl' --image-dir '3.Dataset Construction/Ground Truth/bsv_images_gt_2404' --pdf-dir data/local/pdfs
   python scripts/table_jsonl_reproduction.py rehydrate 'data/Dehydrated Data/Ground Truth/bsv_gt_texttable_final_500_dehydrated.jsonl' data/local/gt_table_tables.jsonl --pdf-dir data/local/pdfs
   ```

   To finish the text–table set, repeat the text command with `data/local/gt_table_tables.jsonl` as input and `3.Dataset Construction/Ground Truth/bsv_gt_texttable_final_merged.jsonl` as output. Check the reported failures before using the outputs.

3. **Construct the training data:** run the automatic construction notebooks for [text–image](3.Dataset%20Construction/Automatic%20Construction/Notebooks/bsv_batch_auto_textimage.ipynb) and [text–table](3.Dataset%20Construction/Automatic%20Construction/Notebooks/bsv_batch_auto_texttable.ipynb). For a new build, use fresh `CHECKPOINT` paths: the included processed-URL files otherwise cause the notebooks to resume previous runs.

Install the dependencies in `pyproject.toml`; experiments require additional model and ML libraries. The ground-truth recipes were generated with PyMuPDF 2.0 and, for whole-table extraction, Docling 2.145.0; rehydration requires the same PDF bytes and compatible extraction versions. Image rehydration extracts the original embedded image using the PDF object ID in its filename and checks the source PDF hash. A bulletin's partial-reproduction statement is not, by itself, a determination of redistribution rights.

The URL list contains 20,215 entries, including 55 additional URLs with shared filenames. The manifest lists 20,160 filenames; two additional `_1` files in the original local corpus duplicate listed PDFs. One alias occurs in the legacy training pairs although the original is held out, so those pairs are not strictly document-disjoint. See the [manifest description](data/manifests/README.md).

## Contribution 1 — Construction pipeline

PhytoPairs is built from French Plant Health Bulletins distributed through the [PHB Knowledge Graph](https://rdf.codex.cati.inrae.fr/bsv-res/sparql). The construction pipeline links each image or table to nearby text in the same document section.

An [interactive reviewer](scripts/generate_reviewer.py) can be generated locally from constructed or rehydrated pairs to inspect, edit, label and export annotations. Generated reviewers can contain source text and images and are not distributed here.

**Extraction and pairing parameters**

| Stage      | Parameter                  | Value                                                |
| ---------- | -------------------------- | ---------------------------------------------------- |
| Headings   | Minimum bold span size     | 10 pt (−0.5 tolerance)                               |
| Headings   | Accepted title length      | 3–79 chars                                           |
| Headings   | Minimum clearance above    | 12 pt                                                |
| Headings   | Same-line span merge gap   | 8 pt vertically                                      |
| Headings   | Adjusted ranking score     | Size +0.5 pt if light, plus rarity bonus             |
| Headings   | Decorative size exclusion  | Count ≤ 2, >1.3× next size                           |
| Headings   | Known non-heading patterns | Rejected (labels, risk levels, dates, credits, etc.) |
| Headings   | Bold span above image      | Rejected                                             |
| Headings   | Maximum heading depth      | 4                                                    |
| Images     | Pages / positions excluded | Cover, 1-page PDFs, and positions above first heading; final-page candidates are included for GT only |
| Images     | Cover-page xrefs           | Dropped by strict parser; GT final-page candidates are added separately |
| Images     | Repeated xref              | Dropped by strict parser (logo/banner)               |
| Images     | Top / bottom page band     | 10% / 10% (both auto and eval)                       |
| Images     | Minimum file size          | 5,000 bytes                                          |
| Images     | Minimum area               | 200k strict / 40k auto / 30k eval                    |
| Images     | Maximum aspect ratio       | 5.5                                                  |
| Images     | Maximum page coverage      | 40%                                                  |
| Images     | Maximum text overlap       | 15%                                                  |
| Images     | Near-white share (>240)    | 80% strict, 45% auto                                 |
| Images     | Near-black share (<15)     | 80%                                                  |
| Images     | Dominant quantised colour  | 70% strict, 45% auto (8 colours)                     |
| Captions   | Italic span below image    | Within 15 pt, 4–200 chars                            |
| Captions   | Prepended title line       | Within 40 pt, non-bold                               |
| Tables     | Shape                      | ≥3 rows, ≥2 columns                                  |
| Tables     | Pages processed            | pdfplumber skips page 1; Docling processes all       |
| Tables     | Mean non-empty cell length | ≤200 chars                                           |
| Tables     | Empty-cell share           | ≤50% strict / Docling; ≤92% loose pdfplumber GT      |
| Tables     | Single-column rows         | ≤60%                                                 |
| Tables     | Column-fill consistency    | ≥2 columns at ≥0.5× max fill                         |
| Tables     | pdf2docx repair guards     | Distinct ≥0.7×, non-empty ≥0.85×, rows ≤2.5×         |
| Assignment | Maximum heading distance   | 250 pt above the image anchor                        |
| Assignment | Same-level image override  | Within ±20 pt                                        |
| Assignment | Local text minimum         | >200 chars for image/table pairs                     |
| Assignment | Chart-noise veto           | >50% numeric/short tokens, or ≤2 words in ≥6 tokens; also rejects named patterns |
| Assignment | Section text minimum       | 50 chars for text modality and text–table pairs      |
| Pairing    | Caption minimum            | 4 chars                                              |
| Pairing    | `Document` sections        | Never paired                                         |

## Contribution 2 — The dataset

A pool of 2,404 PDFs is reserved before construction ([reserve_gt_pdfs.py](3.Dataset%20Construction/Scripts/reserve_gt_pdfs.py), seed 42). The remaining corpus is processed by the batch notebooks in [Automatic Construction/Notebooks/](3.Dataset%20Construction/Automatic%20Construction/Notebooks):

| Pair type  | Pairs  | Bulletins | File                             |
| ---------- | ------ | --------- | -------------------------------- |
| text–image | 34,448 | 9,255     | `bsv_pairs_auto_textimage.jsonl` |
| text–table | 6,757  | 3,919     | `bsv_pairs_auto_texttable.jsonl` |

Each pair carries its `bulletin_id`, so the source document is recoverable for every training and evaluation example.

**Additional corpus statistics**

_Composition of the 41,205 automatic training pairs. Crop categories are derived from filenames; section titles are raw heading strings._

| Crop category    | Pairs | Section title        | Pairs |
| ---------------- | ----: | -------------------- | ----: |
| Arboriculture    | 4,957 | Stades phénologiques |   673 |
| Market gardening | 3,408 | Pucerons             |   425 |
| Vegetables       | 2,955 | Oïdium               |   369 |
| Viticulture      | 2,820 | Punaises phytophages |   215 |
| Field crops      | 1,589 | Thrips               |   196 |

### Ground truth

From the reserved pool, the evaluation sets contain 500 manually curated text–image pairs and 500 text–table pairs. The training pairs are constructed automatically.

## Contribution 3 — Evidence of utility

The [retrieval experiments](4.Finetuning/) compare zero-shot baselines and fine-tuned models using the held-out ground truth. The paper reports mean MRR across both retrieval directions of 0.626 for text–image and 0.564 for text–table. The [citrus case study](5.BBCH%20case%20study/) evaluates transfer to phenological stage prediction. Reference metrics are in [results/](results/).

### Zero-shot retrieval (paper Table 3)

Scores use 500 candidate pairs; chance R@1 is 0.002.

| Setting | Model | Text → other R@1 | Text → other MRR | Other → text R@1 | Other → text MRR |
|---|---|---:|---:|---:|---:|
| Text–image | SigLIP 2, truncate@64 | 0.018 | 0.050 | 0.024 | 0.057 |
| Text–image | SigLIP 2, mean-pool@64 | 0.018 | 0.049 | 0.026 | 0.062 |
| Text–image | CLIP ViT-B/32, truncate@77 | 0.012 | 0.038 | 0.016 | 0.043 |
| Text–image | CLIP ViT-B/32, mean-pool@77 | 0.008 | 0.035 | 0.014 | 0.042 |
| Text–table | TF-IDF + cosine | 0.140 | 0.219 | 0.154 | 0.261 |
| Text–table | Okapi BM25 | 0.048 | 0.074 | 0.120 | 0.200 |
| Text–table | BGE-M3, raw CLS | 0.136 | 0.234 | 0.134 | 0.240 |
| Text–table | Sentence-CamemBERT-Large | 0.074 | 0.131 | 0.094 | 0.173 |
| Text–table | ChouBERT-16, mean-pool | 0.006 | 0.027 | 0.026 | 0.056 |

### Encoder and layer sweep (paper Table 4)

MRR is averaged over both retrieval directions for 2,500 training pairs; `n` is the number of layers unfrozen in each encoder. A dash means the setting was not run.

| Setting | n=0 | n=2 | n=4 | n=6 | n=8 | n=12 |
|---|---:|---:|---:|---:|---:|---:|
| DINOv3 + BGE-M3, text–image | 0.305 | 0.302 | 0.327 | 0.326 | 0.319 | — |
| SigLIP 2, chunk-max | 0.134 | 0.139 | 0.149 | 0.140 | 0.130 | — |
| SigLIP 2, chunk-mean | 0.158 | 0.175 | 0.177 | — | 0.166 | 0.171 |
| BGE-M3, text–table | 0.424 | 0.445 | 0.450 | 0.454 | 0.458 | — |

The paper reports batch size 16 for the text–table sweep and 32 for the others. The saved 27B notebook configures 32 but skips completed runs, so the batch size used for those recorded results is unconfirmed. The SigLIP 2 chunk-mean all-layer result is labelled `n=16` in the historical results; it unfroze all 12 available layers ([notebook](4.Finetuning/27D.textimage_SigLIP%28mean-pooled%29_unfronzen_layers.ipynb)).

### Citrus case study (paper Table 5)

Mean absolute ordinal error in BBCH units for 81 photographs; lower is better. The fine-tuned columns use the retrieval-best checkpoints: SigLIP 2 with 4 unfrozen layers and DINOv3 + BGE-M3 with 6 unfrozen layers and metadata-based mining.

| Stage | Language | Uniform chance | Majority | SigLIP 2 zero-shot | CLIP zero-shot | DINOv3 + BGE-M3 zero-shot | SigLIP 2, 4L | DINOv3 + BGE-M3, mined 6L |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Principal | French | 3.2 | 2.1 | 2.3 | 2.3 | 3.7 | 2.0 | 3.1 |
| Principal | English | 3.2 | 2.1 | 1.8 | 2.3 | 3.4 | 2.5 | 1.9 |
| Secondary | French | 31.8 | 19.8 | 37.0 | 18.7 | 36.2 | 27.7 | 25.8 |
| Secondary | English | 31.8 | 19.8 | 21.3 | 18.3 | 36.0 | 27.2 | 30.5 |

The paper's best principal-stage error of 1.21 uses a different, eight-layer checkpoint.
