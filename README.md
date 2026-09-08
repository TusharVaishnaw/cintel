# Company Intelligence Engine

An AI-powered pipeline that turns a company's SEC filings (10-K / 10-Q) into a fully sourced, provenance-tracked data model — and auto-generates a 28-slide PowerPoint deck from it.

Every fact in the output — every number, every bullet point — carries `{value, source, as_of}`. Nothing is fabricated: if a fact can't be found or computed, the field stays `null` and the slide that would show it is simply skipped.

```
Apple  →  fetch.py → extract_annual_report.py → extract_lists.py → derive.py
       →  enrich.py → quarterly.py → analysis.py
       →  extract_slide_data.py → fetch_photos.py → build_ppt.py
       →  AAPL_deck.pptx
```

One command runs the whole thing:

```bash
python run_pipeline.py "Apple" --year 2025
```

---

## Why this exists

Company research decks — the kind an analyst, sales team, or consultant builds before a pitch or a deal — are usually assembled by hand from 10-Ks, news search, and a market-data terminal. This project automates that: point it at a company name or ticker, and it pulls the real filing, extracts 150+ structured data points, enriches with external sources, synthesizes analytical judgments (SWOT, industry outlook) grounded in the extracted facts, and renders a slide deck — with every fact traceable back to where it came from.

---

## Architecture

The pipeline is a strict linear sequence of stages. Each stage reads the JSON the previous stage wrote, fills in its own section of the schema, and writes back to disk — nothing is passed in memory between stages, since `run_pipeline.py` invokes each one as its own subprocess.

| Stage | Script | What it does | LLM? | Network? |
|---|---|---|---|---|
| 1 | `fetch.py` | Find the company on SEC EDGAR, download the 10-K (or 10-Q) filing HTML + XBRL facts | No | Yes (SEC EDGAR) |
| 2 | `extract_annual_report.py` | Fill `from_annual_report.*` — direct XBRL lookups for financials, LLM extraction (chunked by 10-K Item) for narrative fields | Yes | No |
| 3 | `extract_lists.py` | Fill list-shaped fields — business segments, leadership, acquisitions, partnerships, tech initiatives, challenges | Yes | No |
| 4 | `derive.py` | Compute every `derived.*` field — margins, growth rates, ROE/ROA, FCF, net debt — pure math over stage 2/3 output | No | No |
| 5 | `enrich.py` | Fill `external_enrichment.*` — market cap, competitors, news, awards, federal contracts, leadership photos | No | Yes (yfinance, Google News RSS, Wikipedia, USAspending.gov) |
| 6 | `quarterly.py` | Fill `from_quarterly_report.*` + quarterly YoY growth from the latest 10-Q's XBRL | No | No (reads pre-fetched 10-Q) |
| 7 | `analysis.py` | Synthesize `analysis.swot`, `analysis.industry_indicators`, `analysis.industry_forecast` — LLM judgment over everything extracted/enriched so far | Yes | No |
| 8 | `extract_slide_data.py` | Unwrap the `{value, source, as_of}` schema into a flat `slide_data.json` for the renderer | No | No |
| 9 | `fetch_photos.py` | Download leadership photos to disk (optional — run on a machine with open internet) | No | Yes |
| 10 | `build_ppt.py` | Render the 28-slide `.pptx` from `slide_data.json` using `python-pptx` | No | No |

`field_registry.py` is the single source of truth for who owns which field — `XBRL_MAP`, `LLM_SECTION_MAP`, `DERIVED_MAP`, `EXTERNAL_ONLY`, `ANALYSIS_FIELDS` — so no two stages fight over the same field, and it's obvious at a glance where any given schema leaf gets filled.

### The provenance schema

Every atomic fact in `template_v2.json` is a leaf, not a bare value:

```json
"revenue": {
  "current_year": {"value": 391035000000, "source": "https://sec.gov/...", "as_of": "2025-02-01"}
}
```

Two leaf shapes exist:
- `{value, source, as_of}` — an **extracted** fact (XBRL tag, LLM read of filing text, external API)
- `{value, formula, as_of}` — a **derived/computed** fact (margins, ratios, segment %), where `formula` documents the arithmetic instead of a source URL

Synthesized judgments (SWOT, industry forecast) still use the `source` shape, but with an explicit non-URL string — `"synthesized: LLM analysis over extracted + enriched data"` — so nothing downstream can mistake a model's inference for a sourced fact.

---

## Data sources

| Data | Source | Notes |
|---|---|---|
| Financials | SEC EDGAR XBRL facts | Direct tag lookup, no LLM — deterministic |
| Narrative fields (HQ, strategy, risk factors, etc.) | 10-K text via Gemini (`gemini-3.1-flash-lite`) | Chunked by Item, only the relevant section sent per field |
| Business segments | XBRL `StatementBusinessSegmentsAxis` | Real reportable segments, not product-line prose |
| Market cap, share price | yfinance (Stooq CSV as fallback) | |
| Latest news, awards | Google News RSS | Free, no key; category-tagged by keyword heuristic |
| Competitors | 10-K text (named explicitly) + SEC/yfinance lookup for financials | Falls back to LLM general knowledge, clearly tagged non-filing-verified, only if the filing names none |
| Leadership photos / bio | Wikipedia REST summary API | LinkedIn URL is a constructed search link, not a verified match |
| Federal contracts | USAspending.gov | US-government-vendor companies only |
| SWOT / industry outlook | LLM synthesis over everything extracted so far | Explicitly tagged as synthesized, grounded in real filing/enrichment data, no invented statistics |

Deliberately left null everywhere, on principle: ESG ratings and industry market-size figures have no reliable free/repeatable source — rather than scrape something unreliable, these stay null and are flagged as manual/paid-source-only in `field_registry.py`.

---

## Setup

```bash
pip install edgartools yfinance requests --break-system-packages
pip install python-pptx --break-system-packages   # for the PPT stage
```

Environment variables:

```bash
export GEMINI_API_KEY="your-key-here"
export NEWSAPI_KEY="optional — Google News RSS is used as a free fallback if unset"
```

> **Corporate network note:** this pipeline was built and debugged behind a corporate MITM proxy, which is why several HTTP calls (Gemini, photo downloads) use `verify=False`. That's a workaround specific to that network, not a recommended default — outside that environment, the correct fix is installing the proxy's CA certificate rather than disabling TLS verification.

## Usage

Run the full pipeline:

```bash
python run_pipeline.py "Apple" --year 2025
python run_pipeline.py "Apple" --year 2025 --no-photos   # skip leadership photo download
python run_pipeline.py "Apple" --year 2025 --no-ppt      # stop after template.json, skip deck
```

Or run any stage individually against an already-fetched company (each stage just needs the previous stage's output on disk):

```bash
python fetch.py "Apple" --year 2025
python extract_annual_report.py AAPL 2025
python extract_lists.py AAPL 2025
python derive.py AAPL 2025
python enrich.py AAPL 2025
python analysis.py AAPL 2025
python extract_slide_data.py final/US/AAPL/2025/template.json final/US/AAPL/2025/slide_data.json
python build_ppt.py final/US/AAPL/2025/slide_data.json final/US/AAPL/2025/AAPL_deck.pptx
```

Company lookup accepts a name, and falls back through exact ticker match → substring match → fuzzy match, so `"amzn"`, `"amazon"`, and `"Amazon.com"` all resolve correctly.

### Output layout

```
raw/US/AAPL/2025/filing.html          # raw 10-K
raw/US/AAPL/2025/xbrl_facts.json      # raw XBRL facts
raw/US/AAPL/2025/metadata.json
raw/US/AAPL/2025/quarterly/...        # 10-Q equivalents

extracted/US/AAPL/2025/extracted.json # working file, mutated stage-to-stage

final/US/AAPL/2025/template.json      # final, fully-sourced data model
final/US/AAPL/2025/slide_data.json    # flattened for the renderer
final/US/AAPL/2025/AAPL_deck.pptx     # final deck
```

---

## Deck structure (28 slides)

Title → Company Overview → Mission/Vision/Values → Geographic Presence → Business Segments → Sustainability (2 slides) → Org Structure → Leadership → Technology Team → **SWOT** (overview + 4 detail slides) → **Financials** (annual 3-yr + quarterly) → Key Acquisitions → Key Competitors → Awards → Business Challenges → **Market & Technology** (news, IT spend, deals, tech initiatives, technologies in use) → **Industry Outlook** (indicators + forecast) → Thank You.

Every slide function is data-driven: if the underlying field is empty, the slide (or that slide's row/card) is skipped entirely — never a placeholder, never an empty section header with nothing under it.

---

## Notable engineering problems solved

A non-exhaustive list of bugs that shaped the current design — kept here because each one represents a real failure mode worth knowing about if you're extending this pipeline:

- **Company search matched garbage.** Fuzzy name matching with a loose cutoff (0.3) returned near-random results for tickers (`"amzn"` → nothing sensible) instead of falling through to a better strategy. Fixed with a strict priority order: exact ticker → substring on name → fuzzy match only as a last resort at cutoff 0.6.
- **Headquarters was always null.** Field was mapped to 10-K Item 1, but HQ address actually lives in Item 2 (Properties) — confirmed by grep, not a missing-data issue, a wrong-mapping issue.
- **MSFT's business segments were product lines, not real segments.** Item 1 prose extraction returned "XBOX" / "Search Advertising." The actual reportable segments live in XBRL's business-segment axis (Item 8 footnote) — now pulled directly from there and used to override/validate the narrative pass.
- **Silent truncation dropped real content.** A single global 12,000-char cap on 10-K sections silently cut off Human Capital / geographic disclosures that sit near the end of a 30-50k-char Item 1. Per-Item overrides now give Item 1 / 1A / 7 real headroom (40,000 chars).
- **Zero-retry LLM calls permanently nulled fields.** One bad/empty response from the model — no retry — meant a field that should have been filled stayed null for the rest of that run, with no way to tell "genuinely absent" from "unlucky roll." Retry logic added everywhere a full-section LLM synthesis happens (SWOT, industry indicators, combined-search fields).
- **A flat character cutoff on the whole SWOT context dict silently dropped `business_challenges`** (a major source of threats — regulatory/antitrust risk) because it happened to sit late in dict key order. Fixed by truncating each context section independently instead of the assembled whole.
- **10-Q data landed in the wrong year folder** whenever a 10-Q's own filing year differed from its associated 10-K's year, because the quarterly fetch used its own filing date instead of the caller-resolved annual year — quarterly.py silently found nothing as a result.
- **NVIDIA's capex tag doesn't exist; a "close enough" fallback tag was deliberately rejected** because it only captures a small financed-lease slice (~$101M) versus the real ~$6.1B cash capex — a wrong-but-plausible number is judged worse than an honest null.
- **Fabricated data was actively designed against, not just avoided by omission** — e.g., an early acquisitions bug wrote an explanatory sentence into the `company_name` *value* itself, which would have rendered as a fake company name on the slide. Fixed by keeping the value null and moving the explanation into `source`, which the renderer never displays as data.

---

## Tested against

MSFT, GOOGL, AAPL, JPM, WMT, AMZN — chosen deliberately for filer diversity (bank filing structure, non-standard XBRL tagging, differing Item placement for the same narrative topic across filers).

## Known gaps / open work

- ESG ratings and paid-research-firm industry market-size figures have no free/repeatable source — left null by design.
- `ict_budget` (competitor field) has no free source anywhere — always null.
- LinkedIn URLs for leadership are constructed search links, not verified profile matches — flagged for manual check.
- A Giskard-based groundedness evaluation harness (via `litellm`) exists to check extracted fields against source filing text — not included in this repo slice.

## License

Add your license here.
