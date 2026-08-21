"""
derive.py — Stage 4: compute `derived.*` fields from `from_annual_report.financials`.
Pure math. No LLM, no network. Owns exactly the fields listed in
field_registry.DERIVED_MAP — nothing else.

Usage:
    python derive.py AAPL 2025
(run AFTER extract_annual_report.py + extract_lists.py)
"""

import json
import os
import sys
from datetime import datetime, timezone

from field_registry import DERIVED_MAP

EXTRACTED_DIR = "extracted/US"


def v(node):
    """Pull numeric value out of a value/source/as_of leaf, or None."""
    if not isinstance(node, dict):
        return None
    val = node.get("value")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator, 4)

def pct_growth(current, previous):
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / abs(previous) * 100, 2)

def set_derived(template, key, value, formula):
    template["derived"][key] = {
        "value": value,
        "formula": formula,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

def set_financials_leaf(template, key, value, formula):
    """ebit/ebitda/profit_before_tax live under from_annual_report.financials
    in the schema (not derived.*), but are pure math — computed here, not by
    the LLM stage. Fills the existing leaf's value/source; source note marks
    it as computed so it's distinguishable from a direct XBRL/LLM fill."""
    leaf = template["from_annual_report"]["financials"][key]
    leaf["value"] = value
    leaf["source"] = f"derived: {formula}"
    leaf["as_of"] = datetime.now(timezone.utc).isoformat()


def compute(template):
    fin = template["from_annual_report"]["financials"]

    revenue_cur = v(fin["revenue"]["current_year"])

    # company_profile.revenue is a duplicate slot for the slide-2 overview —
    # alias it to financials.revenue.current_year instead of extracting it
    # twice (was a separate LLM field before, redundant + inconsistent).
    profile_rev = template["from_annual_report"]["company_profile"].get("revenue")
    if isinstance(profile_rev, dict) and revenue_cur is not None:
        rev_leaf = fin["revenue"]["current_year"]
        profile_rev["value"] = revenue_cur
        profile_rev["source"] = f"alias of financials.revenue.current_year ({rev_leaf.get('source')})"
        profile_rev["as_of"] = rev_leaf.get("as_of")
    revenue_prev = v(fin["revenue"]["previous_year"])
    op_income_cur = v(fin["operating_income"]["current_year"])
    op_income_prev = v(fin["operating_income"]["previous_year"])
    net_income_cur = v(fin["net_income"]["current_year"])
    net_income_prev = v(fin["net_income"]["previous_year"])
    gross_profit = v(fin["gross_profit"])
    ocf = v(fin["operating_cash_flow"])
    capex = v(fin["capital_expenditure"])
    total_debt = v(fin["total_debt"])
    cash = v(fin["cash_and_cash_equivalents"])
    equity = v(fin["shareholders_equity"])
    assets = v(fin["total_assets"])
    depreciation_amortization = v(fin.get("depreciation_amortization"))
    income_tax_expense = v(fin.get("income_tax_expense"))

    set_derived(template, "revenue_growth", pct_growth(revenue_cur, revenue_prev),
                DERIVED_MAP["derived.revenue_growth"])
    set_derived(template, "operating_income_growth", pct_growth(op_income_cur, op_income_prev),
                DERIVED_MAP["derived.operating_income_growth"])
    set_derived(template, "net_income_growth", pct_growth(net_income_cur, net_income_prev),
                DERIVED_MAP["derived.net_income_growth"])
    set_derived(template, "operating_margin", ratio(op_income_cur, revenue_cur),
                DERIVED_MAP["derived.operating_margin"])
    set_derived(template, "net_margin", ratio(net_income_cur, revenue_cur),
                DERIVED_MAP["derived.net_margin"])
    set_derived(template, "gross_margin", ratio(gross_profit, revenue_cur),
                DERIVED_MAP["derived.gross_margin"])

    # ebit/ebitda/profit_before_tax — pure math, schema keeps these under
    # financials.* rather than derived.*, so written back into fin directly.
    ebit = op_income_cur  # operating income == EBIT
    set_financials_leaf(template, "ebit", ebit, DERIVED_MAP["derived.ebit"])

    ebitda = None
    if ebit is not None and depreciation_amortization is not None:
        ebitda = round(ebit + depreciation_amortization, 2)
    set_financials_leaf(template, "ebitda", ebitda, DERIVED_MAP["derived.ebitda"])

    profit_before_tax = None
    if net_income_cur is not None and income_tax_expense is not None:
        profit_before_tax = round(net_income_cur + income_tax_expense, 2)
    set_financials_leaf(template, "profit_before_tax", profit_before_tax,
                         DERIVED_MAP["derived.profit_before_tax"])

    # historical_years[].ebitda — extract_annual_report.py fills revenue/
    # operating_income/net_income for historical rows from XBRL directly and
    # explicitly leaves ebitda for this stage (needs D&A, only available
    # after XBRL fill). Never actually implemented — always null. D&A isn't
    # broken out per historical year anywhere in this pipeline, so use the
    # same current-year D&A rate as a proxy, flagged as such in source.
    hist = fin.get("historical_years")
    if isinstance(hist, list) and depreciation_amortization is not None:
        for row in hist:
            if not isinstance(row, dict):
                continue
            row_op_income = v(row.get("operating_income"))
            row_ebitda_leaf = row.get("ebitda")
            if row_op_income is not None and isinstance(row_ebitda_leaf, dict) and row_ebitda_leaf.get("value") is None:
                row_ebitda_leaf["value"] = round(row_op_income + depreciation_amortization, 2)
                row_ebitda_leaf["source"] = (
                    "derived (approximate): historical_year.operating_income + "
                    "CURRENT year depreciation_amortization (no per-year D&A available "
                    "in this filing's XBRL comparatives) — " + DERIVED_MAP["derived.ebitda"]
                )
                row_ebitda_leaf["as_of"] = datetime.now(timezone.utc).isoformat()

    fcf = None
    if ocf is not None and capex is not None:
        fcf = round(ocf - capex, 2)
    set_derived(template, "free_cash_flow", fcf, DERIVED_MAP["derived.free_cash_flow"])

    net_debt = None
    if total_debt is not None and cash is not None:
        net_debt = round(total_debt - cash, 2)
    set_derived(template, "net_debt", net_debt, DERIVED_MAP["derived.net_debt"])

    set_derived(template, "roe", ratio(net_income_cur, equity), DERIVED_MAP["derived.roe"])
    set_derived(template, "roa", ratio(net_income_cur, assets), DERIVED_MAP["derived.roa"])
    set_derived(template, "debt_to_equity", ratio(total_debt, equity),
                DERIVED_MAP["derived.debt_to_equity"])
    current_assets = v(fin["current_assets"])
    current_liabilities = v(fin["current_liabilities"])
    set_derived(template, "current_ratio", ratio(current_assets, current_liabilities),
                DERIVED_MAP["derived.current_ratio"])

    # segment revenue % — per segment, needs total revenue
    segments = template["from_annual_report"]["business_segments"]["segments"]
    if revenue_cur:
        for seg in segments:
            seg_rev = v(seg.get("revenue"))
            pct = round(seg_rev / revenue_cur * 100, 2) if seg_rev is not None else None
            seg["revenue_percentage"] = {
                "value": pct,
                "formula": DERIVED_MAP["derived.segment_revenue_percentage"],
                "as_of": datetime.now(timezone.utc).isoformat(),
            }

    # quarterly growth — needs from_quarterly_report to be filled separately
    q = template.get("from_quarterly_report", {})
    q_rev_cur = v(q.get("revenue"))
    template["derived"]["quarterly_revenue_growth"] = {
        "value": None,  # requires prior-year same-quarter figure, not in single quarter fetch
        "formula": DERIVED_MAP["derived.quarterly_revenue_growth"],
        "as_of": None,
    }
    template["derived"]["quarterly_net_income_growth"] = {
        "value": None,
        "formula": DERIVED_MAP["derived.quarterly_net_income_growth"],
        "as_of": None,
    }

    return template


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python derive.py TICKER YEAR")
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    path = os.path.join(EXTRACTED_DIR, ticker, year, "extracted.json")
    if not os.path.exists(path):
        raise SystemExit(f"Run extract_annual_report.py + extract_lists.py first — {path} missing")

    with open(path, encoding="utf-8") as f:
        template = json.load(f)

    template = compute(template)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"Derived fields updated: {path}")


if __name__ == "__main__":
    main()
