"""
quarterly.py — Stage 3c (optional): fills from_quarterly_report.* from the
latest 10-Q's XBRL facts, plus derived.quarterly_revenue_growth /
quarterly_net_income_growth. Same tool (edgartools) as fetch.py, same XBRL
read pattern as extract_annual_report.py — no LLM needed, these are all
standard XBRL tags same as the annual ones.

Usage:
    python fetch.py "..." --form 10-Q --cik <cik> --ticker <ticker>   (run first)
    python quarterly.py TICKER YEAR
(YEAR = the annual report year this quarter's data attaches to, same as
 every other stage — reads raw/US/TICKER/YEAR/quarterly/xbrl_facts.json)
"""

import json
import os
import sys
from datetime import datetime, timezone, date

from extract_annual_report import _lookup
from derive import pct_growth

RAW_DIR = "raw/US"
EXTRACTED_DIR = "extracted/US"

QUARTERLY_XBRL_MAP = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "operating_income": "OperatingIncomeLoss",
    "net_income": "NetIncomeLoss",
    "eps": "EarningsPerShareDiluted",
    "cash_flow": "NetCashProvidedByUsedInOperatingActivities",
}


def _parse_date(s):
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


def load_quarterly_xbrl_facts(path):
    """10-Q facts are NOT tagged fiscal_period=='FY' (that's an annual-only
    label) — reusing extract_annual_report.load_xbrl_facts() here silently
    returns nothing every time, which is why revenue/op_income/etc all came
    back null on the first run despite fetch.py pulling real 10-Q XBRL data.

    A 10-Q also reports BOTH the single quarter AND the year-to-date
    duration as separate facts sharing the same period_end — picking
    "latest period_end" alone risks grabbing the 9-month YTD number instead
    of the 3-month quarter. Prefer duration facts closest to ~90 days;
    fall back to the single longest available duration per concept only if
    no quarter-length fact exists for it.

    A 10-Q ALSO includes the prior-year SAME quarter as a comparative
    column (same as how the annual filing carries 2-3 years of income
    statement comparatives) — already sitting in this same file. Returns
    (current_facts, prior_year_facts) so quarterly YoY growth is
    computable without an extra fetch."""
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        records = json.load(f)

    duration_facts = [
        r for r in records
        if r.get("period_type") == "duration" and not r.get("is_dimensioned", False)
        and r.get("period_end") and r.get("concept")
    ]
    if not duration_facts:
        return {}, {}

    by_concept = {}
    for r in duration_facts:
        concept = r["concept"].split(":")[-1]
        start, end = _parse_date(r.get("period_start")), _parse_date(r.get("period_end"))
        length_days = (end - start).days if start and end else None
        val = r.get("numeric_value")
        if val is None:
            val = r.get("value")
        if val is None:
            continue
        by_concept.setdefault(concept, []).append((length_days, r.get("period_end"), val))

    facts, prior_facts = {}, {}
    for concept, rows in by_concept.items():
        # prefer a quarter-length fact (60-100 days) over a YTD one; among
        # ties, prefer the most recent period_end
        quarter_rows = [row for row in rows if row[0] is not None and 60 <= row[0] <= 100]
        pool = quarter_rows or rows
        pool.sort(key=lambda row: row[1] or "", reverse=True)
        facts[concept] = pool[0][2]

        cur_end = _parse_date(pool[0][1])
        if not cur_end:
            continue
        # same-quarter-prior-year row: ~1 year earlier period_end (340-390
        # day gap covers fiscal-calendar drift without matching a totally
        # different quarter)
        for _length, end_str, val in pool[1:]:
            end_date = _parse_date(end_str)
            if end_date and 340 <= (cur_end - end_date).days <= 390:
                prior_facts[concept] = val
                break

    return facts, prior_facts


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python quarterly.py TICKER YEAR")
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    raw_dir = os.path.join(RAW_DIR, ticker, year, "quarterly")
    xbrl_path = os.path.join(raw_dir, "xbrl_facts.json")
    meta_path = os.path.join(raw_dir, "metadata.json")
    if not os.path.exists(xbrl_path):
        print(f"[warn] {xbrl_path} missing — run fetch.py --form 10-Q first. Skipping quarterly stage.")
        return

    with open(meta_path) as f:
        meta = json.load(f)
    source_url = meta["source_url"]
    as_of = meta["filing_date"]

    current_facts, prior_facts = load_quarterly_xbrl_facts(xbrl_path)
    if not current_facts:
        print("[warn] no usable duration facts found in quarterly xbrl_facts.json — leaving from_quarterly_report null")

    extracted_path = os.path.join(EXTRACTED_DIR, ticker, year, "extracted.json")
    if not os.path.exists(extracted_path):
        raise SystemExit(f"Run the annual pipeline first — {extracted_path} missing")
    with open(extracted_path, encoding="utf-8") as f:
        template = json.load(f)

    q = template["from_quarterly_report"]
    q["quarter"] = {"value": meta.get("period_of_report"), "source": source_url, "as_of": as_of}
    for field, tag in QUARTERLY_XBRL_MAP.items():
        val = _lookup(current_facts, tag)
        if val is not None:
            q[field] = {"value": val, "source": source_url, "as_of": as_of}

    # ebitda: same proxy pattern as derive.py — no per-quarter D&A tag
    # confirmed reliable, left null unless op_income present and a
    # depreciation_amortization tag happens to exist for the quarter.
    da_val = _lookup(current_facts, "DepreciationDepletionAndAmortization") or \
        _lookup(current_facts, "DepreciationAmortizationAndOther")
    op_income = q["operating_income"].get("value")
    if op_income is not None and da_val is not None:
        q["ebitda"] = {"value": round(op_income + da_val, 2), "source": f"derived (approximate): {source_url}", "as_of": as_of}

    # quarterly YoY growth — computable from THIS SAME file's prior-year
    # comparative quarter, no extra fetch needed. derive.py (runs earlier
    # in the pipeline, before quarterly data exists) leaves these two
    # derived.* fields null as placeholders; overwrite them here now that
    # both quarters are available.
    rev_cur = _lookup(current_facts, QUARTERLY_XBRL_MAP["revenue"])
    rev_prior = _lookup(prior_facts, QUARTERLY_XBRL_MAP["revenue"])
    ni_cur = _lookup(current_facts, QUARTERLY_XBRL_MAP["net_income"])
    ni_prior = _lookup(prior_facts, QUARTERLY_XBRL_MAP["net_income"])
    growth_as_of = datetime.now(timezone.utc).isoformat()
    if rev_cur is not None and rev_prior is not None:
        template["derived"]["quarterly_revenue_growth"] = {
            "value": pct_growth(rev_cur, rev_prior),
            "formula": "yoy % change on quarterly revenue (same-quarter prior year, from 10-Q comparatives)",
            "as_of": growth_as_of,
        }
    if ni_cur is not None and ni_prior is not None:
        template["derived"]["quarterly_net_income_growth"] = {
            "value": pct_growth(ni_cur, ni_prior),
            "formula": "yoy % change on quarterly net income (same-quarter prior year, from 10-Q comparatives)",
            "as_of": growth_as_of,
        }

    with open(extracted_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"Quarterly data updated: {extracted_path}")


if __name__ == "__main__":
    main()
