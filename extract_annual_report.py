"""
extract_annual_report.py — Stage 3: raw filing -> filled from_annual_report section.

- XBRL_MAP fields: read straight from xbrl_facts.json. No LLM.
- LLM_SECTION_MAP fields: filing.html split by 10-K Item, only the relevant
  chunk sent to local model (never the full 300-400 page doc).
- Fields in EXTERNAL_ONLY / DERIVED_MAP / ANALYSIS_FIELDS: skipped here,
  owned by later stages.

Usage:
    python extract_annual_report.py AAPL 2025
"""

import copy
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

from field_registry import XBRL_MAP, XBRL_FALLBACKS, LLM_SECTION_MAP

# handled separately by fill_sustainability() — searches Item 1/1A/7
# combined instead of one fixed Item (location varies by filer)
LLM_SECTION_MAP_SUSTAINABILITY_FIELDS = ["from_annual_report.sustainability"]

RAW_DIR = "raw/US"
EXTRACTED_DIR = "extracted/US"
TEMPLATE_PATH = "template_v2.json"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ITEM_PATTERN = re.compile(
    r"item\s+(1a|1b|1c|1|2|7a|7|8|9a|9b|9|10|11|12|13|14)\b[\.\:]?\s*",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# template helpers
# ---------------------------------------------------------------------------
def load_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def set_leaf(obj, dotted, value, source, as_of):
    parts = dotted.split(".")
    node = obj
    try:
        for key in parts[:-1]:
            node = node[key]
        leaf = node[parts[-1]]
    except (KeyError, TypeError):
        print(f"[warn] invalid field path '{dotted}', skipping")
        return
    if isinstance(leaf, dict) and "value" in leaf:
        leaf["value"] = value
        leaf["source"] = source
        leaf["as_of"] = as_of
    else:
        # container (dict/list of sub-objects) — caller fills manually
        node[parts[-1]] = value


def get_path(obj, dotted):
    node = obj
    for key in dotted.split("."):
        node = node[key]
    return node




# ---------------------------------------------------------------------------
# XBRL direct fill — no LLM
# ---------------------------------------------------------------------------
def load_xbrl_facts(path):
    if not os.path.exists(path):
        print(f"[warn] no xbrl_facts.json at {path}")
        return {}, {}, {}, None, {}
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)

    # income-statement facts: period_type "duration", fiscal_period "FY", dated by period_end
    duration_ends = [
        r.get("period_end") for r in records
        if r.get("period_type") == "duration" and r.get("fiscal_period") == "FY"
        and not r.get("is_dimensioned", False) and r.get("period_end")
    ]
    # balance-sheet facts: period_type "instant", no fiscal_period, dated by period_instant.
    # max() would grab a stray cover-page date (e.g. shares-outstanding-as-of) that only
    # 1-2 facts share — use the date with the MOST facts instead, that's the real balance sheet date.
    from collections import Counter
    instant_dates = [
        r.get("period_instant") for r in records
        if r.get("period_type") == "instant"
        and not r.get("is_dimensioned", False) and r.get("period_instant")
    ]
    date_counts = Counter(instant_dates)

    if not duration_ends and not instant_dates:
        print("[warn] no consolidated facts found")
        return {}, {}, {}, None, {}

    latest_duration = max(duration_ends) if duration_ends else None
    prior_durations = sorted({e for e in duration_ends if e < latest_duration}, reverse=True) if latest_duration else []
    prior_duration = prior_durations[0] if prior_durations else None
    # second-prior year — 10-Ks carry 3 fiscal years of comparatives on the
    # income statement (current + 2 prior). Previously only 2 were collected.
    second_prior_duration = prior_durations[1] if len(prior_durations) > 1 else None

    latest_instant = date_counts.most_common(1)[0][0] if date_counts else None
    prior_instants = sorted({e for e in instant_dates if e < latest_instant}, reverse=True) if latest_instant else []
    # among dates before latest_instant, pick the one with most facts (real prior balance sheet date)
    if prior_instants:
        prior_candidates = Counter(e for e in instant_dates if e < latest_instant)
        prior_instant = prior_candidates.most_common(1)[0][0]
    else:
        prior_instant = None

    def collect(duration_end, instant_date):
        facts = {}
        for r in records:
            if r.get("is_dimensioned", False):
                continue
            concept = r.get("concept")
            if not concept:
                continue
            ptype = r.get("period_type")
            if ptype == "duration":
                if r.get("fiscal_period") != "FY" or r.get("period_end") != duration_end:
                    continue
            elif ptype == "instant":
                if r.get("period_instant") != instant_date:
                    continue
            else:
                continue
            tag = concept.split(":")[-1]
            val = r.get("numeric_value")
            if val is None:
                val = r.get("value")
            if val is not None:
                facts[tag] = val
        return facts

    current = collect(latest_duration, latest_instant)
    prior = collect(prior_duration, prior_instant)
    # instant_date irrelevant for a pure income-statement year — pass None,
    # collect() only matches instant facts when ptype == "instant" and date equal;
    # second-prior balance sheet snapshot isn't part of the 3-yr comparative anyway.
    second_prior = collect(second_prior_duration, None) if second_prior_duration else {}

    # geographic revenue — filing has a real "revenue by country" table but it's
    # only present as DIMENSIONED facts (srt:StatementGeographicalAxis), which
    # collect() above deliberately skips (dimensioned = segment/geo/customer
    # breakdowns, not the consolidated total). Pull it separately here, keyed
    # by country name, current fiscal year only (latest_duration match).
    # Filtered to the income-statement role specifically — the same axis also
    # appears on an unrelated "long-lived assets by region" table with facts
    # that have no period_end (None), which the period_end match excludes anyway.
    geo_revenue = {}
    for r in records:
        if not r.get("dim_srt_StatementGeographicalAxis"):
            continue
        if r.get("period_end") != latest_duration:
            continue
        if r.get("concept") not in ("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"):
            continue
        val = r.get("numeric_value")
        label = r.get("dimension_member_label")
        if val is not None and label:
            geo_revenue[label] = val

    return current, prior, second_prior, second_prior_duration, geo_revenue


def _lookup(facts, tag):
    if "+" in tag:
        parts = tag.split("+")
        vals = [facts.get(p) for p in parts]
        return sum(vals) if all(v is not None for v in vals) else None
    val = facts.get(tag)
    if val is None:
        for fallback in XBRL_FALLBACKS.get(tag, []):
            val = facts.get(fallback)
            if val is not None:
                break
    return val


def fill_from_xbrl(template, current_facts, prior_facts, second_prior_facts, source_url, as_of):
    for field_path, tag in XBRL_MAP.items():
        value = _lookup(current_facts, tag)
        if value is not None:
            set_leaf(template, field_path, value, source_url, as_of)
        # also fill matching .previous_year / .two_years_prior siblings for
        # revenue/operating_income/net_income (3-yr income statement comparatives)
        if field_path.endswith(".current_year"):
            prev_path = field_path.replace(".current_year", ".previous_year")
            prev_value = _lookup(prior_facts, tag)
            if prev_value is not None:
                set_leaf(template, prev_path, prev_value, source_url, as_of)

            two_prior_path = field_path.replace(".current_year", ".two_years_prior")
            two_prior_value = _lookup(second_prior_facts, tag)
            if two_prior_value is not None:
                set_leaf(template, two_prior_path, two_prior_value, source_url, as_of)

    # capex proxy fallback — some filers (confirmed: NVDA) have no direct
    # cash-flow-statement capex tag at all in their XBRL facts. If the normal
    # XBRL_MAP lookup left capital_expenditure null, approximate it from
    # gross PP&E year-over-year delta instead of leaving it blank. This is
    # NOT the exact filed cash-capex figure — flagged clearly in source.
    capex_leaf = template["from_annual_report"]["financials"]["capital_expenditure"]
    if capex_leaf.get("value") is None:
        ppe_cur = _lookup(current_facts, "PropertyPlantAndEquipmentGross")
        ppe_prior = _lookup(prior_facts, "PropertyPlantAndEquipmentGross")
        if ppe_cur is not None and ppe_prior is not None:
            capex_leaf["value"] = round(ppe_cur - ppe_prior, 2)
            capex_leaf["source"] = (
                f"approximated (no direct XBRL capex tag): "
                f"PropertyPlantAndEquipmentGross delta, filing={source_url}"
            )
            capex_leaf["as_of"] = as_of


# ---------------------------------------------------------------------------
# split filing.html into 10-K Items — LLM only ever sees one chunk at a time
# ---------------------------------------------------------------------------
def html_to_text(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&amp;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


SIGNATURE_MARKER = re.compile(
    r"pursuant to the requirements of the securities exchange act.{0,200}?signed below",
    re.IGNORECASE | re.DOTALL,
)


def split_items(html):
    text = html_to_text(html)
    matches = list(ITEM_PATTERN.finditer(text))

    # Confirmed this session (MSFT): SEC filing HTML repeats an item's
    # header text as a running page header on EACH printed page of a
    # multi-page section. Every repeat matches ITEM_PATTERN as if it were
    # a fresh section start, fragmenting one real Item into several pieces.
    # The old "keep only the longest fragment" logic silently discarded
    # every other fragment — including, confirmed by inspection, the
    # OVERVIEW/results-of-operations fragment that actually opens Item 7
    # and is where strategy language lives; a later, unrelated Liquidity
    # fragment happened to be longer and won by that rule. Concatenate all
    # fragments for the same item instead, in document order, so nothing
    # gets silently dropped just because it wasn't the single longest run.
    fragments = {}
    for i, m in enumerate(matches):
        item_no = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if len(chunk) > 200:  # skip TOC hits (too short to be real section)
            key = f"Item {item_no}"
            fragments.setdefault(key, []).append(chunk)

    # Confirmed this session: raw document order caused a regression —
    # partnerships extraction (MSFT) dropped from 2 items to 0 after
    # switching to concatenation. A short spurious early match (a running
    # page-header repeat or stray cross-reference) landed at the FRONT of
    # the joined chunk, pushing the real Item 1 content — including the
    # OEM-partnerships paragraph — past the char-limit truncation. Sort
    # fragments longest-first: the real section (almost always the longest
    # fragment) leads, so truncation still cuts from genuinely secondary
    # content, while shorter real fragments (like the MD&A overview that
    # motivated concatenation in the first place) still get appended after.
    items = {
        key: "\n\n".join(sorted(chunks, key=len, reverse=True))
        for key, chunks in fragments.items()
    }

    # signature page: names/titles of officers+directors live here, not in
    # Item 10 (which is usually "incorporated by reference" to the proxy
    # statement and has no real names). Grab it directly by its own marker.
    sig_match = SIGNATURE_MARKER.search(text)
    if sig_match:
        items["Signatures"] = text[sig_match.end():sig_match.end() + 6000].strip()

    return items


# ---------------------------------------------------------------------------
# LLM extraction — one call per Item, only fields mapped to that Item
# ---------------------------------------------------------------------------
def fields_by_item():
    grouped = {}
    for field_path, item in LLM_SECTION_MAP.items():
        grouped.setdefault(item, []).append(field_path)
    return grouped


import time

import certifi
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_last_call_time = [0.0]
MIN_INTERVAL = 4.5  # 15 RPM free tier -> stay under it


def call_llm(prompt, retries=1):
    if not GEMINI_API_KEY:
        raise SystemExit("Set GEMINI_API_KEY env var first.")
    elapsed = time.time() - _last_call_time[0]
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    # temperature=0: run-to-run field coverage was swinging
                    # (mission_vision_values 3/3 -> 1/3, company_strategy
                    # 8/8 -> 6/8 on the SAME filing text, same prompt) —
                    # default sampling temp, not a real extraction bug.
                    # Pin deterministic decoding so re-runs are comparable
                    # and null vs filled reflects the SOURCE, not dice rolls.
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
                },
                timeout=60,
                verify=False,  # corporate network MITM proxy breaks cert chain
            )
            _last_call_time[0] = time.time()
            if resp.status_code != 200:
                print(f"[error] gemini HTTP {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (requests.exceptions.RequestException, KeyError, IndexError) as e:
            print(f"[error] gemini call exception: {e}")
            if attempt < retries:
                print("[warn] retrying...")
                time.sleep(2)
                continue
            print("[warn] giving up on this call")
            return "{}"


MAX_CHARS = 12000  # keep chunk well within local model context
# Item 1 (Business) commonly runs 30-50k chars and Human Capital / geographic
# footprint paragraphs sit near the END of the section (after product/segment
# description) — truncating at MAX_CHARS silently drops employees/countries
# every time. gemini-3.1-flash-lite handles far larger prompts; give Item 1
# real headroom instead of raising the global limit for every section.
ITEM_MAX_CHARS = {
    "Item 1": 40000,
    # Risk Factors runs 20-40+ pages in large filers (NVIDIA confirmed) —
    # material risks (e.g. country-specific regulatory/antitrust items) can
    # sit well past the old 12000-char cutoff and get silently dropped before
    # ever reaching the challenges list. Same class of bug as Item 1.
    "Item 1A": 40000,
    # Confirmed this session (MSFT): company_strategy fields mapped to Item 7
    # (MD&A) came back 1/8 filled — only the field near the START of the
    # section landed, everything else (corporate_strategy, growth_strategy,
    # digital_strategy, technology_strategy, market_expansion_strategy,
    # management_outlook) came back null. MD&A commonly runs 20-50k+ chars
    # for a filer this size and was never added to this override — still
    # hitting the 12000 default. Same class of bug as Item 1/1A, just missed
    # on this Item until now.
    "Item 7": 40000,
}


def max_chars_for(item_name):
    return ITEM_MAX_CHARS.get(item_name, MAX_CHARS)


def extract_section(item_name, chunk_text, field_paths, max_chars=None):
    chunk_text = chunk_text[:(max_chars if max_chars is not None else max_chars_for(item_name))]
    field_list = "\n".join(f"- {f}" for f in field_paths)
    prompt = f"""You extract facts from a 10-K section. Return ONLY valid JSON,
keys = field path, value = extracted fact (string, or null if not found).
Do not invent data.

Fields to extract from {item_name}:
{field_list}

Section text:
\"\"\"{chunk_text}\"\"\"

JSON:"""
    raw = call_llm(prompt)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[warn] LLM returned non-JSON for {item_name}, skipping")
        return {}


def expand_field_paths(template, field_paths):
    """Dict-container fields (multiple leaves, not a list) get expanded into
    their actual leaf sub-paths so they're extracted directly. True list
    fields ([]) pass through untouched for extract_lists.py to handle."""
    expanded = []
    for fp in field_paths:
        node = get_path(template, fp)
        if isinstance(node, dict) and "value" in node:
            expanded.append(fp)  # already a leaf
        elif isinstance(node, dict):
            expanded.extend(f"{fp}.{k}" for k in node.keys())  # container -> leaves
        else:
            expanded.append(fp)  # list field, untouched
    return expanded


def _coerce_llm_dict(result, label):
    """Same shape-safety as fill_from_llm's guard: LLM sometimes returns a
    JSON list instead of {field: value}. Unwrap a single-dict list, else
    return {} so callers' .values()/.items() never crash — an empty dict
    reads as 'nothing extracted' and falls into the existing null/retry
    path instead of raising."""
    if isinstance(result, list):
        if len(result) == 1 and isinstance(result[0], dict):
            return result[0]
        print(f"[warn] {label}: LLM returned a list not a dict, treating as empty")
        return {}
    if not isinstance(result, dict):
        print(f"[warn] {label}: LLM returned unexpected type {type(result).__name__}, treating as empty")
        return {}
    return result


def fill_from_llm(template, items_text, source_url, as_of):
    grouped = fields_by_item()
    # Fields handled separately below via fill_combined_search() — mapping
    # to a single fixed Item is fragile: which Item a topic lives in
    # varies by filer (confirmed for all 4: sustainability MSFT=Item1 vs
    # Alphabet=1A/7; company_strategy MSFT=Item7 vs Apple/Alphabet=Item1;
    # mission/vision Item1 works for some, not others; it_spending same
    # pattern). A fixed single-Item mapping silently returns null for any
    # company that doesn't happen to use that Item for the topic.
    COMBINED_SEARCH_PREFIXES = (
        "from_annual_report.sustainability",
        "from_annual_report.company_strategy",
        "from_annual_report.mission_vision_values",
        "from_annual_report.it_spending",
    )
    for item_name, field_paths in grouped.items():
        field_paths = [f for f in field_paths if not f.startswith(COMBINED_SEARCH_PREFIXES)]
        if not field_paths:
            continue
        chunk = items_text.get(item_name)
        if not chunk:
            print(f"[warn] {item_name} not found in filing, skipping {len(field_paths)} fields")
            continue
        field_paths = expand_field_paths(template, field_paths)
        # only extract leaf-style fields here; container fields (segments[],
        # people[] etc) need their own list-aware prompt — flagged, not silently skipped
        leaf_fields = [f for f in field_paths if is_leaf(template, f)]
        list_fields = [f for f in field_paths if f not in leaf_fields]
        if leaf_fields:
            for i in range(0, len(leaf_fields), 12):
                batch = leaf_fields[i:i + 12]
                result = extract_section(item_name, chunk, batch)
                # Confirmed bug (JPM crash): LLM sometimes returns a JSON
                # list instead of the expected {field: value} dict — seen
                # on bank filings with unusual Item structure. Never
                # crashed before because no shape check existed.
                result = _coerce_llm_dict(result, item_name)
                for field_path, value in result.items():
                    if field_path not in batch:
                        print(f"[warn] LLM returned unknown field '{field_path}', ignoring")
                        continue
                    if value is not None:
                        set_leaf(template, field_path, value, source_url, as_of)
        for lf in list_fields:
            print(f"[todo] list field '{lf}' needs list-aware extractor — see extract_lists.py")

    fill_combined_search(template, items_text, source_url, as_of,
                          ["from_annual_report.sustainability"], "sustainability")
    fill_combined_search(template, items_text, source_url, as_of,
                          ["from_annual_report.company_strategy"], "company_strategy")
    fill_combined_search(template, items_text, source_url, as_of,
                          ["from_annual_report.mission_vision_values"], "mission_vision_values")
    fill_combined_search(template, items_text, source_url, as_of,
                          ["from_annual_report.it_spending"], "it_spending")


def fill_combined_search(template, items_text, source_url, as_of, field_prefixes, label):
    """Searches Item 1, 1A, and 7 combined instead of committing to one
    fixed Item — the topic's actual location varies by filer, and a single
    fixed mapping silently returns null for whichever company doesn't use
    that Item for it."""
    field_paths = expand_field_paths(template, field_prefixes)
    leaf_fields = [f for f in field_paths if is_leaf(template, f)]
    if not leaf_fields:
        return

    PER_ITEM_CHARS = 15000  # smaller share each since combining 3 items
    combined = ""
    for item_name in ("Item 1", "Item 1A", "Item 7"):
        chunk = items_text.get(item_name, "")
        if chunk:
            combined += f"\n\n--- {item_name} ---\n" + chunk[:PER_ITEM_CHARS]
    if not combined.strip():
        print(f"[warn] no Item 1/1A/7 text found, skipping {label}")
        return

    for i in range(0, len(leaf_fields), 12):
        batch = leaf_fields[i:i + 12]
        # Confirmed bug (originally found on sustainability, same fix
        # applies to every combined-search field): without an explicit
        # cap, extract_section() falls back to the global 12000-char
        # default (this pseudo item name isn't in ITEM_MAX_CHARS) and
        # RE-truncates the already-assembled ~45000-char combined text
        # down to 12000 — cutting off right after the Item 1 header,
        # before any real content. Pass the actual assembled length
        # through explicitly instead.
        #
        # Confirmed 2nd bug (MSFT mission_vision_values/sustainability
        # regressed to 0/3 on a rerun): this was a single LLM call with
        # zero retry — one bad/incomplete response (Gemini non-determinism
        # on a sparse, scattered topic across 45k chars) permanently
        # nulled the field, no second attempt. Retry up to 2x if the
        # result comes back completely empty for this batch before
        # accepting null.
        result = extract_section(f"Item 1/1A/7 combined ({label} search)", combined, batch, max_chars=len(combined))
        result = _coerce_llm_dict(result, label)
        if not any(v is not None for v in result.values()):
            for attempt in range(2):
                print(f"[retry] {label}: empty result, attempt {attempt + 2}/3")
                result = extract_section(f"Item 1/1A/7 combined ({label} search)", combined, batch, max_chars=len(combined))
                result = _coerce_llm_dict(result, label)
                if any(v is not None for v in result.values()):
                    break
        for field_path, value in result.items():
            if field_path not in batch:
                continue
            if value is not None:
                set_leaf(template, field_path, value, source_url, as_of)


def is_leaf(template, dotted):
    node = get_path(template, dotted)
    return isinstance(node, dict) and "value" in node


# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python extract_annual_report.py TICKER YEAR")
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    raw_dir = os.path.join(RAW_DIR, ticker, year)
    with open(os.path.join(raw_dir, "metadata.json")) as f:
        meta = json.load(f)
    with open(os.path.join(raw_dir, "filing.html"), encoding="utf-8") as f:
        html = f.read()
    current_facts, prior_facts, second_prior_facts, second_prior_end, geo_revenue = load_xbrl_facts(
        os.path.join(raw_dir, "xbrl_facts.json")
    )

    source_url = meta["source_url"]
    as_of = meta["filing_date"]

    template = load_template()
    set_leaf(template, "metadata.company", meta["company"], source_url, as_of)
    set_leaf(template, "metadata.cik", meta["cik"], source_url, as_of)
    set_leaf(template, "metadata.ticker", meta["ticker"], source_url, as_of)
    set_leaf(template, "metadata.annual_report_url", source_url, source_url, as_of)
    set_leaf(template, "metadata.annual_report_year", meta["filing_date"][:4], source_url, as_of)
    set_leaf(template, "metadata.extraction_date", datetime.now(timezone.utc).isoformat(), "system", None)

    fill_from_xbrl(template, current_facts, prior_facts, second_prior_facts, source_url, as_of)

    # geographic_revenue — dimensioned XBRL data, filled directly (not via
    # XBRL_MAP/LLM) since it's a country->value dict, not a single number.
    if geo_revenue:
        set_leaf(template, "from_annual_report.geographic_presence.geographic_revenue",
                 geo_revenue, source_url, as_of)

    # historical_years[0] — same second-prior year, list-shaped leaf.
    # Filled directly here since it's pulled straight from XBRL, not the LLM.
    # ebitda left for derive.py (needs D&A, computed after this stage).
    if second_prior_facts:
        hist = get_path(template, "from_annual_report.financials.historical_years")
        if isinstance(hist, list) and hist:
            row = hist[0]
            row_tag_map = {
                "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
                "operating_income": "OperatingIncomeLoss",
                "net_income": "NetIncomeLoss",
            }
            for key, tag in row_tag_map.items():
                if key not in row:
                    continue
                val = _lookup(second_prior_facts, tag)
                if val is not None:
                    row[key]["value"] = val
                    row[key]["source"] = source_url
                    row[key]["as_of"] = as_of
            if second_prior_end and "year" in row:
                row["year"]["value"] = second_prior_end[:4]
                row["year"]["source"] = source_url
                row["year"]["as_of"] = as_of

    items_text = split_items(html)
    print(f"Found sections: {list(items_text.keys())}")
    fill_from_llm(template, items_text, source_url, as_of)

    out_dir = os.path.join(EXTRACTED_DIR, ticker, year)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "extracted.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
