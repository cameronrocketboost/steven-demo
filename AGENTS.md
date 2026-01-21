### Steven Demo — Agent Notes (What we did + What’s next)

This repo is a **local-first quoting demo** aligned to Sensei’s “fast price updates + accurate quotes” messaging.
It ingests Coast-to-Coast **Price Book PDFs**, extracts tables/rules, normalizes them into a clean schema, and drives a **Streamlit quoting UI**.

---

### Current goals

- **Demo-ready quoting** from manufacturer price books with:
  - clear itemized output
  - traceability (“price book revision used”)
  - quick “price update” story (swap price book / revision)
- **Local recording friendly**: Streamlit UI is local; extraction can use Mistral OCR via URL upload.

---

### What we built (completed)

- **PDF extraction pipeline (Mistral OCR + structuring)**
  - File: `scripts/extract_pricebooks.py`
  - Output per PDF:
    - `out/<pdf_stem>/ocr_raw.json`
    - `out/<pdf_stem>/ocr_text.md`
    - `out/<pdf_stem>/pricebook_extracted.json` (rules + markdown tables)
  - Key update: Mistral OCR now expects `document.type=document_url`, so we upload PDFs to a temporary URL first.

- **Supabase Storage upload provider for OCR URLs**
  - Implemented in: `scripts/extract_pricebooks.py`
  - Env-driven:
    - `MISTRAL_UPLOAD_PROVIDER=supabase`
    - `SUPABASE_URL=...`
    - `SUPABASE_ANON_KEY=...`
    - `SUPABASE_BUCKET=mistral-tmp` (optional)
    - `DELETE_AFTER_OCR=true` (default)
  - Robustness improvement:
    - After upload, we test the **public URL**. If it’s not accessible, we automatically fall back to a **signed URL** (valid ~10 minutes) and pass that to Mistral.
    - Best-effort delete runs after OCR if `DELETE_AFTER_OCR=true`.

- **Visible progress during structuring**
  - The structuring loop displays `tqdm` chunk progress, so runs don’t look stuck.

- **Local quoting engine (pure Python)**
  - File: `pricing_engine.py`
  - Capabilities:
    - base price lookup by (style, roof, gauge, width, length)
    - leg-height add-on pricing
    - options priced by length (with **next-length-up fallback** when needed)
    - itemized line items + total
    - “placement” tag for options (FRONT/BACK/LEFT/RIGHT) to support section-based UX
    - commercial-size coverage (demo): if requested size exceeds extracted matrix, we **extrapolate** beyond max instead of silently pricing at max
      - adds explicit line item: `COMMERCIAL_SIZE_EXTRAP` + traceability notes
    - demo rules:
      - supports **Regular** + **A-Frame** and **Horizontal** + **Vertical** roof styles
      - enforces: **Vertical roof only available on A-Frame**
      - adds a visible **lift required** note for **13' or taller**
      - handles the **“vertical buildings are 1' shorter”** rule so option pricing can still map to the corresponding horizontal length column when needed
  - Tests:
    - `tests/test_pricing_engine.py` (unittest discovery passes)

- **Streamlit demo app**
  - File: `local_demo_app.py`
  - Uses **R29 (NW) normalized pricebook** as the demo source and builds a composite demo book (Regular + A-Frame horizontal + A-Frame vertical).
  - Implements “section” navigation (mirrors Sensei-style workflow conceptually):
    - Built & Size
    - Leg Height
    - Doors & Windows
    - Options
    - Colors
    - Notes
  - Demo polish:
    - “Golden quotes” sidebar presets (one-click scenarios for recording)
  - Reliability / UX improvements:
    - explicit **opening placement editor** (doors/windows/garage): wall (FRONT/BACK/LEFT/RIGHT) + offset; drives drawings + priced line items
    - robust session persistence across Streamlit reruns using **shadow state** + **wizard checkpoints**
    - fixed a “selection resets / didn’t add” issue by treating `openings` as an active state key, persisting `opening_seq`, and clamping offsets after size changes
    - sidebar terms (demo): manufacturer discount (%) + downpayment (%) flow into PDF totals

- **Quote artifacts (PDF + drawings)**
  - Files: `quote_pdf.py`, `building_views.py`
  - Multi-page PDF export:
    - Page 1: header + customer + building summary + itemized line items + vendor-style totals
    - Pages 2–6: building views (Front / Back / Left / Right / Isometric)
  - Building drawings:
    - isometric + elevation views
    - door/window/garage openings are rendered with wall placement + offsets
    - roof seam lines for better depth cues; improved scaling (building fills most of the canvas)

- **Normalization step (“clean + structured”)**
  - Script: `scripts/normalize_pricebooks.py`
  - Reads: `out/**/pricebook_extracted.json` + associated `ocr_text.md`
  - Writes: `out/<pdf_stem>/normalized_pricebook.json`
  - Behavior:
    - Parses markdown tables into canonical structures:
      - `base_matrices[]` (entries: width/length/price + gauge + allowed widths/lengths)
      - `option_tables[]` (option_prices_by_code + leg_height_addons)
    - Detects useless OCR text (e.g. only dots) and writes:
      - `status="invalid_ocr_text"` with a reason (prevents polluting demo)

- **Normalized pricebook loader + builder**
  - File: `normalized_pricebooks.py`
  - Loads `normalized_pricebook.json` and constructs a `PriceBook` the engine can use.
  - Adds `build_demo_pricebook_r29()` to merge the R29 base matrices needed for the demo (Regular/A-Frame horizontal + A-Frame vertical) plus `OPTION LIST` and accessories.

---

### What’s in `out/` right now

- R29: extracted + structured + normalized (usable)
- R30: extracted + structured + normalized (usable)
- R31: extracted + structured + normalized, but **marked invalid** because OCR text was just “.” (not usable)

---

### How to run (commands)

#### Install deps (local machine)

```bash
python3 -m pip install -r requirements.txt
```

#### Extract + structure PDFs (requires network + Mistral + upload URL provider)

Example (single PDF):

```bash
python3 "/Users/cameron/STEVEN DEMO/scripts/extract_pricebooks.py" \
  --input-dir "/Users/cameron/STEVEN DEMO" \
  --output-dir "/Users/cameron/STEVEN DEMO/out" \
  --pdf "Coast_To_Coast_Carports___Price_Book___R29 (1).pdf"
```


#### Normalize extracted outputs (local-only)

```bash
python3 "/Users/cameron/STEVEN DEMO/scripts/normalize_pricebooks.py" \
  --out-dir "/Users/cameron/STEVEN DEMO/out"
```

#### Run Streamlit demo (local)

```bash
python3 -m streamlit run "/Users/cameron/STEVEN DEMO/local_demo_app.py"
```

#### Run tests (recommended before demo)

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

#### Generate the vendor-style demo artifact (optional smoke check)

```bash
python3 "/Users/cameron/STEVEN DEMO/scripts/simulate_vendor_demo_quote.py"
```

---

### Known issues / current limitations

- **R31 OCR failure**: OCR output is effectively empty (“.”). Normalization marks it invalid. We need to fix OCR for this PDF (see “Next work”).
- **Schema is still MVP**:
  - Many extracted option tables contain multiple sub-sections (e.g. both-sides-closed, closed ends, etc.) that are not yet modeled as first-class “wall/side section” pricing rules.
- **Commercial pricing caveat (still demo-level)**:
  - Sizes beyond the extracted base matrix are priced via **extrapolation** (explicit line item + notes), but option/leg-height tables are still priced using the closest available length column.
- **Vendor “commercial” option coverage gap**: vendor line items like “commercial closed-sides/ends” and “chain-hoist garage door” are not yet modeled (so dollar parity can still differ).
- **No full manufacturer comparison** yet (but the data model can support multiple revisions).

---

### Next work (prioritized)

#### 1) Make normalization fully “CPQ-clean”
- Enhance `scripts/normalize_pricebooks.py` to classify option tables into typed structures:
  - base matrices by category (A-Frame / Regular / Vertical roof, etc.)
  - option sections:
    - ground certification
    - j-trim
    - snow/wind loads
    - leg-height add-ons
    - both-sides-closed
    - closed ends (per end)
    - vertical sides/ends add-ons, etc.
- Add stronger parsing validation + diagnostics output:
  - counts of parsed entries
  - missing lengths/widths warnings
  - example lookup checks (“golden quotes”)

#### 2) Fix R31 extraction
- Identify why OCR returns only dots:
  - PDF corruption/encoding/scan settings
  - try repaired PDF or different OCR settings / endpoint model
- Goal: produce real tables, then remove `invalid_ocr_text` state.

#### 3) Expand “section-based” UX to match Sensei-style builder flow
- Add additional sections in `local_demo_app.py`:
  - Lean To’s (placement + dimensions)
  - Sides & Ends (side selection + closure types)
  - Doors & Windows (wall selection + opening type/position)
  - Colors (cosmetic)
- Replace the current simple placement picker with a clearer “structure map” UI:
  - select FRONT/BACK/LEFT/RIGHT visually
  - tie selected placement to line items

#### 4) Quote artifacts
- “Save quote” snapshot (inputs + pricebook revision + totals)
  - v1 exists as lead snapshots in `leads/leads.jsonl` (local demo artifact)
  - next: add a first-class “Save quote” button and/or export folder structure
- PDF export (logo + line items + totals + revision used)
  - implemented multi-page PDF + drawing pages
  - next: tighten visual parity (fonts/spacing) and add more vendor fields (wind/snow rating, on-center spacing) if needed for sales motion

#### 5) Price updates demo flow
- Add “switch revision” / “apply % bump” to show “fast updates”:
  - generate quote on R29
  - switch to R30 (or updated version) and re-quote instantly
  - show delta by line item

#### 6) Vendor simulation runner (screenshot fixture)
- `scripts/simulate_vendor_demo_quote.py` writes:
  - `out/vendor_sim/demo_quote_sim.pdf`
  - `out/vendor_sim/demo_quote_sim_preview.png`
  - `out/vendor_sim/demo_quote_sim_report.json`

---

### Repo files of interest

- Extraction:
  - `scripts/extract_pricebooks.py`
  - `config.example.json`
- Normalization:
  - `scripts/normalize_pricebooks.py`
  - `normalized_pricebooks.py`
- Demo app:
  - `local_demo_app.py`
- Pricing:
  - `pricing_engine.py`
  - `tests/test_pricing_engine.py`

---

### Action plan (new): Produce a PDF quote artifact like the screenshots + run a comparison simulation

#### Goal
- Generate a **downloadable PDF quote** that matches the *shape* of the screenshoted vendor quote (header, customer/details, itemized table, totals box, notes, and drawing pages), and then run a **side-by-side comparison** between that vendor PDF’s values and our engine’s quote.

Status: **Mostly complete for the demo** (PDF export + drawing pages + vendor-style totals + a simulation runner). Remaining parity gaps are primarily commercial-only option pricing and fuller vendor “terms/rules” coverage.

#### Tasks (implementation) + success criteria

1) **Define a “Quote Artifact” data model (single source of truth)**
   - **Work**:
     - Create a Python `QuoteArtifact` structure (dataclass or TypedDict) that includes:
       - metadata: quote_id, date, salesperson/contact, dealer/company branding
       - customer: name/email/address (optional), jobsite flags (installation ready, permit, etc.)
       - building: width/length/height, roof style, gauge, wind/snow rating, on-center spacing
       - colors: roof/trim/sides
       - line_items: description, qty, unit price (optional), extended price
       - totals: subtotal, discounts, additional charges, grand total, down payment, balance due
       - traceability: `pricebook_revision` (already available) + any normalization notes used
       - drawings: pointers to generated images (thumbnail + optional multi-page views)
   - **Success criteria**:
     - Given a `QuoteResult` + Streamlit state, we can construct a `QuoteArtifact` deterministically.
     - Artifact includes **pricebook revision traceability** and **all line items + totals** required for rendering.

2) **Implement PDF rendering (v1: single-page like screenshot page 1)**
   - **Work**:
     - Add a PDF generator (recommendation: **ReportLab** for local-only, no system deps).
     - Layout includes:
       - top header block with logo + company + salesperson + quote number + date + total
       - customer details block
       - building summary block (dimensions, roof style, gauge, rating, spacing)
       - itemized table with Qty + amount aligned right
       - totals box (subtotal/discount/charges/grand total/down payment/balance due)
       - notes area
   - **Success criteria**:
     - In Streamlit, user can click **“Download quote (PDF)”** and receive a valid PDF.
     - PDF includes: **logo**, **quote id**, **date**, **pricebook revision**, **line items**, **grand total**.
     - Visual parity target: clearly recognizable as the screenshot layout (not pixel-perfect).

3) **Generate drawing assets (v1: simple but credible)**
   - **Work**:
     - Produce a “building thumbnail” image for page 1 (simple render is fine for demo).
     - Produce additional pages:
       - 3D-ish views: FRONT/BACK/LEFT/RIGHT (can be stylized placeholders driven by dimensions/colors)
       - a 2D plan view with labeled sides and placed openings (garage door / walk-in / windows)
     - Store generated images in-memory for PDF embed and optionally export as PNG for debugging.
   - **Success criteria**:
     - PDF can optionally include pages 2–6 with views/plan.
     - Drawings update based on inputs (at minimum: width/length/height + openings count/placement).

4) **Map our current inputs → vendor-quote concepts**
   - **Work**:
     - Add/standardize fields that appear in the vendor PDF but aren’t first-class in our UI yet:
       - wind/snow rating selection (even if demo-stubbed to a few presets)
       - on-center spacing (e.g. 5 ft)
       - manufacturer discount (as either % or fixed amount)
       - down payment and balance due calculation rules (demo rule: configurable %)
     - Ensure these fields flow into `QuoteArtifact` and PDF.
   - **Success criteria**:
     - We can reproduce the vendor PDF’s “header summary row” fields and totals box fields.
     - Discount/down payment values are deterministic and shown in the PDF.

5) **Build the “simulation” comparator (vendor PDF → our quote)**
   - **Work**:
     - Create a repeatable way to ingest the vendor quote values:
       - v1: a hand-authored JSON fixture representing the vendor PDF values (dimensions, options, line item amounts, totals)
       - v2 (optional): parser that extracts text/tables from the vendor PDF into that JSON fixture
     - Implement a comparison runner that:
       - maps vendor fixture → our `QuoteInput` + option selections
       - generates our `QuoteResult`
       - outputs a diff report: per-line-item deltas + total delta, and highlights missing/extra items
   - **Success criteria**:
     - Running one command prints a clear comparison table and a single “total delta” number.
     - For the provided demo vendor PDF, we can explain any mismatch as one of:
       - missing pricing coverage (not in our normalized book yet)
       - mapping ambiguity (vendor item doesn’t map 1:1 to our option codes)
       - intentional demo simplification (documented)

6) **Lock it down with tests**
   - **Work**:
     - Add unit tests that validate:
       - `QuoteArtifact` construction correctness (includes revision, totals, stable quote_id)
       - PDF renderer returns non-empty bytes and includes expected text markers
       - comparator produces expected deltas for a known fixture
   - **Success criteria**:
     - `python -m unittest` passes locally.
     - Regression protection exists for the “demo quote” scenario.


