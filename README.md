## Steven Demo – Price Book Extraction

This folder contains Coast-to-Coast “Price Book” PDFs and a small script to extract their contents into **demo-ready JSON** (rules + tables + notes) using **Mistral Document AI / OCR** and a second Mistral “structuring” pass.

This extraction is the first step toward a quoting demo aligned to Sensei Digital’s “fast price updates + accurate quotes” pitch ([feature page](https://www.senseidigital.com/fast-price-updates-accurate-quotes/)).

### What this produces

For each PDF in the input directory, the script writes:

- **`out/<pdf_stem>/ocr_raw.json`**: The raw OCR response.
- **`out/<pdf_stem>/ocr_text.md`**: Best-effort extracted text/markdown.
- **`out/<pdf_stem>/pricebook_extracted.json`**: A structured JSON summary with:
  - `rules`: important pricing rules / constraints
  - `tables`: markdown tables detected (base grids, add-ons, etc.)
  - `notes`: misc notes / disclaimers

### Setup

- **Install deps**:

```bash
python3 -m pip install -r /Users/cameron/STEVEN\ DEMO/requirements.txt
```

- **Create a config file**:
  - Copy `config.example.json` to `config.json`
  - Set your `mistral_api_key`
  - If your Mistral OCR docs specify a different endpoint/model, update `ocr.endpoint` / `ocr.model`

### Run extraction

```bash
python3 /Users/cameron/STEVEN\ DEMO/scripts/extract_pricebooks.py \
  --config /Users/cameron/STEVEN\ DEMO/config.json \
  --input-dir /Users/cameron/STEVEN\ DEMO \
  --output-dir /Users/cameron/STEVEN\ DEMO/out
```

To **only OCR** and skip the structuring step:

```bash
python3 /Users/cameron/STEVEN\ DEMO/scripts/extract_pricebooks.py \
  --config /Users/cameron/STEVEN\ DEMO/config.json \
  --input-dir /Users/cameron/STEVEN\ DEMO \
  --output-dir /Users/cameron/STEVEN\ DEMO/out \
  --no-structure
```

### Notes

- These PDFs appear to be difficult to parse reliably with common local libraries, so this workflow relies on Mistral’s OCR/Document AI.
- After extraction, we can normalize the markdown tables into a proper **Price Book schema** (base matrices + options + rule constraints) for the quoting demo described in `Comversation.md`.

### Local quoting demo (Streamlit)

For recording a **local-only** demo (no external services), run the Streamlit app:

```bash
python3 -m pip install -r /Users/cameron/STEVEN\ DEMO/requirements.txt
python3 -m streamlit run /Users/cameron/STEVEN\ DEMO/local_demo_app.py
```

This demo currently uses a **hardcoded sample price book** built from the R29 screenshots (`sample_pricebook_r29.py`) and generates a single itemized quote using `pricing_engine.py`.


