# Company Intelligence Engine — Interview Q&A (40)

## Architecture / Pipeline Design

**1. Walk me through the pipeline end to end.**
fetch.py (SEC EDGAR 10-K/10-Q via edgartools) → extract_annual_report.py (XBRL direct + LLM per-Item) → extract_lists.py (list-shaped fields: segments, people, acquisitions) → derive.py (pure math) → enrich.py (external sources) → analysis.py (LLM synthesis: SWOT/indicators/forecast) → extract_slide_data.py (unwrap to flat slide_data.json) → fetch_photos.py (optional) → build_ppt.py (28-slide deck). run_pipeline.py orchestrates all of it as one command.

**2. Why split extraction into so many stages instead of one big script?**
Each stage has a hard dependency on the previous one's output and a distinct failure mode. derive.py needs financials filled; analysis.py needs derive.py + enrich.py both done since SWOT is a judgment over everything, not extraction from one section. Keeping them separate means a bug in one stage doesn't require rerunning stages that already succeeded, and each stage can be tested/rerun independently against the same extracted.json on disk.

**3. Why do intermediate stages read and rewrite the same JSON file instead of passing data in memory?**
Statelessness across process boundaries. run_pipeline.py invokes each stage as a subprocess (`subprocess.run`), so persistence to disk is the only way to hand off state. It also means you can rerun any single stage manually against an existing extracted.json without rerunning the whole pipeline.

**4. What's the {value, source, as_of} leaf pattern and why does every field use it?**
Every atomic fact in the schema is wrapped as a dict with its value, where it came from, and when it was fetched — a "zero-fabrication" provenance contract. It lets downstream consumers (and a human reviewer) trace any number on the final slide back to an XBRL tag, an LLM call plus its source URL, or an external API, rather than trusting an opaque number.

**5. Two leaf shapes exist — {value,source,as_of} and {value,formula,as_of}. Why?**
Extracted facts (from a filing or external source) get a `source` — a URL or tag name. Computed/derived fields (derive.py's math, segment revenue_percentage) get a `formula` describing how they were computed instead, since there's no external source URL for arithmetic. extract_slide_data.py's `unwrap()` had to be fixed to match both shapes; it originally only matched the `source` variant and left every derived.* field as an un-unwrapped dict.

**6. Why does run_pipeline.py treat PPT generation as non-fatal but treat earlier stages as fatal?**
Each pipeline stage from fetch through analysis feeds the next — a failure early on means every downstream file is built on incomplete data, so it aborts immediately (`run_step`). PPT generation is a rendering step over an already-complete, already-saved template.json/slide_data.json; a bad photo URL or one missing node shouldn't invalidate research data that's already correct and saved. `run_step_optional` catches this by only warning, never exiting.

---

## Data Fetching / XBRL

**7. Why fetch XBRL facts directly instead of just letting the LLM read the financial statements?**
XBRL is structured, standardized, machine-readable filer-reported data — pulling `RevenueFromContractWithCustomerExcludingAssessedTax` directly is exact and deterministic. Sending 300-400 pages of prose to an LLM for numbers that are already tagged is slower, costs tokens, and introduces transcription risk for figures that don't need any interpretation at all.

**8. How does the pipeline pick between duration facts (income statement) and instant facts (balance sheet)?**
Duration facts are matched on `period_type == "duration"` + `fiscal_period == "FY"`, keyed by `period_end`; instant facts are matched by `period_type == "instant"`, keyed by `period_instant`. For instant facts specifically, `max()` on the date was wrong — it could grab a stray cover-page date (e.g., a shares-outstanding-as-of date) shared by only 1-2 facts. Fixed by picking the instant date with the *most* facts attached (via Counter), since that's the real balance sheet date.

**9. Why does the fetcher collect a "second prior" fiscal year, and what was wrong before?**
10-Ks report 3 fiscal years of income-statement comparatives (current + 2 prior), but the original version only collected 2. Fixed by sorting all prior duration_ends descending and taking the top two, so `historical_years[0]` in the schema can be filled directly from XBRL rather than left null.

**10. Why is geographic revenue pulled in a separate code path from the rest of XBRL?**
It only exists as *dimensioned* facts (tagged with `dim_srt_StatementGeographicalAxis`) — a country→value breakdown — while the main `collect()` function deliberately skips all dimensioned facts (those are segment/geo/customer breakdowns, not the consolidated total the rest of the schema wants). Geo revenue is pulled with its own loop, filtered to the income-statement concept specifically (to exclude an unrelated "long-lived assets by region" table sharing the same axis).

**11. Why did NVIDIA's capital_expenditure come back null even with a fallback tag technically available?**
NVDA doesn't tag cash capex under the primary concept or the standard fallback. A real NVDA-specific tag exists (`PaymentsForFinancedPropertyPlantAndEquipment...`) but it only covers a small financed-lease slice (~$101M) versus the ~$6.1B the filing text actually states. Using it as a fallback would silently produce a wrong-but-plausible number — worse than null — so it was deliberately left out. extract_annual_report.py's PP&E-gross-delta proxy step fills capex instead when the direct lookup misses.

**12. Why does D&A use a fallback tag for Microsoft specifically?**
`DepreciationDepletionAndAmortization` is entirely absent from MSFT's XBRL facts (confirmed directly against the raw file). MSFT instead tags the combined cash-flow-statement line as `DepreciationAmortizationAndOther`. It was added as a fallback — verified against real FY values ($38.5B/$29.4B/$21.0B) — with the caveat that "and other" non-cash items may push the number slightly high versus a filer that breaks out pure D&A separately.

**13. Employees isn't in XBRL_MAP — why?**
Confirmed absent as a standard XBRL tag in Apple's facts. It's extracted via the LLM from Item 1 narrative text instead (LLM_SECTION_MAP), since headcount is disclosed in prose, not tagged financial data.

**14. Walk through the company search fix in fetch.py — what was broken and why?**
The original version only fuzzy-matched against company *name* with `difflib.get_close_matches(cutoff=0.3)`. Typing a ticker like "amzn" never matched anything real, and 0.3 is loose enough that it almost never returns zero matches — so instead of falling through to a better strategy, it returned near-random noise (e.g., "Sanofi" for "netflix"). Rewritten as a strict priority order: (1) exact ticker match, (2) substring match on company name, (3) fuzzy match only as a last resort with cutoff raised to 0.6.

**15. Why does fetch.py write the 10-Q into the *annual* filing's year folder instead of the 10-Q's own filing year?**
Confirmed bug: using the 10-Q's own `filing_date.year` put quarterly data in a different folder than the annual data whenever the two filings' calendar years differ (e.g., Apple's 10-K filed 2025, a later 10-Q filed 2026) — quarterly.py looked in `.../2025/quarterly/` but data landed in `.../2026/quarterly/`, so it silently found nothing. Fixed by using the caller-resolved annual `year` argument for the quarterly output path too, so both stages agree on which folder holds a given company's data.

---

## LLM Extraction Strategy

**16. Why is the filing chunked by 10-K Item instead of sent whole to the LLM?**
Cost, context limits, and precision — a 10-K can run 300-400 pages, and most fields live in one predictable Item. LLM_SECTION_MAP maps each field to its Item number so only the relevant chunk is sent, keeping prompts small and reducing the chance of the model pulling the wrong fact from an unrelated section.

**17. Why did headquarters come back null for every company originally?**
It was mapped to Item 1, but HQ address/facility footprint actually sits in Item 2 (Properties) — confirmed by grep, Item 1 never mentions the HQ city/state at all for MSFT. The field was field-mapped to the wrong Item, not missing from the filing; fixing the mapping fixed the extraction.

**18. Some fields (sustainability, company_strategy, mission_vision_values, it_spending) aren't in LLM_SECTION_MAP at all — why?**
Their location varies by filer — confirmed MSFT has company_strategy in Item 7 while Apple/Alphabet put it in Item 1; sustainability appears in Item 1 for MSFT but Item 1A/7 for Alphabet. A single fixed Item mapping would silently return null for whichever company doesn't use that Item. `fill_combined_search()` searches Item 1+1A+7 combined instead of committing to one Item.

**19. What two bugs did fill_combined_search have, and how were they fixed?**
First: without an explicit char cap, `extract_section()` fell back to a 12000-char default (since the pseudo item name "combined search" wasn't in ITEM_MAX_CHARS) and re-truncated the already-assembled ~45000-char combined text down to 12000 — cutting content right after the Item 1 header, before any real content. Fixed by passing `max_chars=len(combined)` explicitly. Second: it was a single LLM call with zero retry — one bad/incomplete response permanently nulled the whole field on a rerun (confirmed on MSFT: mission_vision_values/sustainability regressed to 0/3). Fixed with up to 2 retries when the batch result comes back completely empty.

**20. Why does analysis.py have its own separate retry wrapper (call_llm_json_retry) instead of reusing whatever extract_annual_report.py has?**
Same root bug class — a single bad/empty LLM response with zero retry permanently nulling a whole section (SWOT, indicators, forecast, industry_market_data) — confirmed as the same failure mode already fixed in `fill_combined_search`. Given analysis.py runs last and each of its outputs is a whole slide's content (not a small leaf field), the cost of one silent null is much higher, so it gets its own dedicated retry-with-validation wrapper checking for valid, non-empty JSON.

**21. What was the JPM crash and how was it fixed?**
The LLM sometimes returns a JSON *list* instead of the expected `{field: value}` dict — seen specifically on bank filings with unusual Item structure. Calling `.items()` on a list crashes. `_coerce_llm_dict()` was added as a shape-check/normalization step before iterating, since no such check existed originally.

**22. Why does synthesize_swot truncate each context section independently instead of truncating the whole JSON blob to one length?**
The prior version did `json.dumps(context)[:10000]` — a flat character cutoff on the whole context dict. `business_challenges` (a major source of threats: regulatory/antitrust risk) sits late in dict key order and was getting sliced off entirely before reaching the model. Truncating each section independently to 3000 chars guarantees every category gets *some* representation regardless of where it falls in the dict.

**23. Why does the prompt in synthesize_swot specifically call out business_challenges?**
Because that section is the primary source for weaknesses/threats, and a single disclosed regulatory/antitrust risk sitting as one item in that section is easy for the model to omit in favor of more numerous, more "obvious" points elsewhere. The prompt explicitly instructs the model not to omit those even if there's only one.

**24. How does the pipeline avoid presenting a synthesized SWOT/forecast as if it were extracted fact?**
Every synthesized leaf's `source` field is set to an explicit string ("synthesized: LLM analysis over extracted + enriched data") rather than a URL. Same pattern for the general-knowledge competitor fallback in enrich.py, tagged "LLM general knowledge — NOT filing-verified" — so downstream consumers and the schema itself can distinguish a judgment call from a sourced fact.

**25. Why is industry_market_data (forecast + growth_drivers) filled by analysis.py's LLM synthesis instead of a real market-research API?**
No confirmed free structured API exists for market-size/forecast data — only paid research firms (Gartner, IDC, Statista) carry it. Rather than leave the field permanently null, the same LLM-judgment-grounded-in-filing-context pattern used for `industry_indicators.growth_rating` is applied here, explicitly labeled as synthesized so it's never confused with a scraped market report figure.

---

## Segments / Lists

**26. Why did MSFT's business segments come back as "XBOX" and "Search Advertising" instead of the real reporting segments?**
The original Item-1-only LLM pass extracted product lines mentioned in prose, not the filer's actual reportable segments (Productivity and Business Processes / Intelligent Cloud / More Personal Computing). That's wrong data, not just incomplete data — Item 1 describes products, Item 8's segment footnote (tagged via `us-gaap:StatementBusinessSegmentsAxis`) is the real reporting structure. Fixed by pulling real per-segment revenue/operating income from that XBRL axis and using it to replace/validate the narrative pass.

**27. How does segment revenue percentage get computed, and where does it live in the schema?**
derive.py computes `seg_rev / financials.revenue.current_year * 100` per segment, once total revenue is known — it's a `{value, formula, as_of}` leaf (computed, not extracted) attached directly onto each segment object rather than living in the top-level `derived.*` namespace, since it's inherently per-segment.

**28. How does acquisitions extraction avoid duplicate entries when both the Item 1 narrative and XBRL find the same deal?**
A dollar-value matching heuristic: XBRL acquisition amounts are rounded to the nearest million and compared against LLM-extracted values already in the narrative-pass rows; if within 5%, it's treated as the same deal (no append). There's also an Item 8 (Business Combinations footnote) cross-match by value that fills in a missing `company_name` on an existing row rather than creating a duplicate.

**29. What was the "named-but-valueless" acquisition bug?**
Rows where the LLM found a company name in prose (VIZIO, Flipkart, PhonePe) but no nearby dollar figure were never checked against XBRL rows — only *unnamed* rows were compared via the value-matching set. Fixed by trying to fill an existing named-but-valueless row's missing value from an XBRL row first, but only when exactly one such row is ambiguous; with 2+ candidates it leaves both null rather than guessing which name pairs with which number.

**30. Why does the pipeline never write an explanatory sentence into company_name for an unnamed acquisition?**
An earlier version did exactly that — the explanatory text would literally render as the "company name" on the Key Acquisitions slide, since the renderer just prints whatever's in that field. Fixed by leaving the value null (honestly reflecting that the counterparty isn't stated) and putting the explanation in the `source` field instead, which downstream renderers never display as data.

**31. How does extract_lists.py filter the Signatures block down to a clean leadership list?**
The raw extraction pulls every signer with type/designation/appointment_date, then `filter_officers()` keeps only rows whose designation substring-matches CEO, CFO ("Principal Financial Officer"), or Chief Accounting Officer ("Principal Accounting Officer"), dropping other directors and non-principal signers, and reshapes to the template's final 5-field people[] shape.

---

## derive.py / Computed Fields

**32. Why is derive.py a completely separate stage with no LLM and no network calls?**
Everything it computes is pure arithmetic on already-extracted financials (margins, growth rates, ratios). Keeping it LLM-free and network-free makes it deterministic, fast, and trivially testable — same inputs always produce the same outputs, unlike the LLM stages.

**33. ebit/ebitda/profit_before_tax live under from_annual_report.financials, not derived.* — why, and how are they filled?**
Schema design choice: they're conceptually part of "the financials," not a separately-derived analytical metric like margin ratios. derive.py still computes them (EBIT = operating income; EBITDA = EBIT + D&A; PBT = net income + tax expense) but writes them back into the existing financials leaf via `set_financials_leaf()`, tagging the source as "derived: <formula>" so it's distinguishable from a direct XBRL or LLM fill even though it lives in the same section.

**34. How is historical_years[].ebitda computed, and what approximation does it rely on?**
It was flagged in extract_annual_report.py as intentionally left for derive.py (needs D&A, unavailable at that earlier stage) but never actually implemented before — always null. Since D&A isn't broken out per historical year anywhere in the pipeline, derive.py uses the *current year's* D&A rate as a proxy against each historical row's operating income, and explicitly flags this in the source string as an approximation using current-year D&A on a prior year's data.

**35. Why does derive.py alias company_profile.revenue to financials.revenue.current_year instead of extracting it separately?**
It used to be a separate LLM-extracted field, which was redundant (same fact, two extraction paths) and could produce inconsistent values between the two. derive.py now just copies the value/source/as_of straight from `financials.revenue.current_year` after XBRL fill, with the source string noting it's an alias — one source of truth, no duplicate extraction cost.

---

## Enrichment / External Sources

**36. Why was DuckDuckGo dropped entirely from the enrichment stage?**
It was rate-limit blocking every run. Latest news and awards were moved to Google News RSS (free, no key required); investor_information kept using yfinance; competitors moved to LLM-named-from-filing-text + SEC ticker lookup + yfinance financials.

**37. Why does enrich.py maintain a manual COMPANY_ALIASES dict for a handful of tickers?**
Confirmed bug: GOOGL's SEC legal name "Alphabet Inc." cleans to "Alphabet," but most press coverage says "Google" — the news search's quoted-phrase filter excluded legitimate Google-branded articles because it only searched "Alphabet," dropping latest_news from 44 results down to 3. The alias dict is keyed by ticker (stable across corporate renames) rather than by name.

**38. How does the pipeline distinguish a genuinely-zero-competitors filing from a fetch failure?**
It's explicit two-stage logic: first try to extract competitors explicitly named in Item 1/1A text — if the LLM finds zero, that's logged as "0 named explicitly in the filing (correct null, not a fetch failure)" and stored as an empty list. Only then, as an opt-in fallback, does it ask the LLM's general knowledge for widely-recognized competitors — and every row from that fallback is tagged with a source string that says "NOT filing-verified," so it can never be silently mistaken for filing-sourced data downstream.

**39. Why does fetch_photos.py exist as a separate script instead of being folded into enrich.py?**
The sandbox/dev environment this pipeline runs debugging in has network access locked to package registries only — it can't reach arbitrary photo URLs. fetch_photos.py is meant to run on a separate machine with real internet access, between extract_slide_data.py and build_ppt.py, downloading each photo to disk and writing back a `photo_path` field. If a download fails (dead link, timeout, non-image response), `photo_path` is simply left unset and the renderer falls back to an initials avatar — never blocks the rest of the deck.

**40. Why does fetch_photos.py use verify=False in its requests call, and is that a concern?**
The comment documents it as a workaround for a corporate MITM proxy in the dev environment, consistent with the same SSL-bypass pattern used elsewhere in the project (e.g., the global `ssl.create_default_context` monkeypatch needed because `google-genai` builds its own SSL context internally). It's a known, deliberate trade-off for working inside that specific network — worth being upfront in an interview that `verify=False` is not something you'd ship to a production environment outside that constraint, and the fix there would be installing the proxy's CA cert instead of disabling verification.
