"""
analysis.py — Stage 5: synthesize `analysis.swot` and `analysis.industry_indicators`.

Runs LAST — needs from_annual_report + derived + external_enrichment already
filled, since SWOT/indicators are judgments drawn across all of it, not
extraction from a single section.

Usage:
    python analysis.py AAPL 2025
"""

import json
import os
import sys
from datetime import datetime, timezone

from extract_annual_report import call_llm

EXTRACTED_DIR = "extracted/US"
FINAL_DIR = "final/US"


def call_llm_json_retry(prompt, label, retries=2):
    """Single LLM call with zero retry meant one bad/empty response
    permanently nulled a whole analysis section (SWOT, indicators,
    forecast, industry_market_data) — same class of bug confirmed in
    extract_annual_report.fill_combined_search. Retry up to `retries`
    times if the response isn't valid non-empty JSON before giving up."""
    for attempt in range(retries + 1):
        raw = call_llm(prompt)
        try:
            parsed = json.loads(raw)
            if parsed:
                return parsed
        except json.JSONDecodeError:
            pass
        if attempt < retries:
            print(f"[retry] {label}: empty/invalid JSON, attempt {attempt + 2}/{retries + 1}")
    print(f"[warn] {label} synthesis returned non-JSON after {retries + 1} attempts")
    return None


def leaf_values(node):
    """Strip value/source wrappers down to plain values for prompt context."""
    if isinstance(node, dict):
        if "value" in node and set(node.keys()) <= {"value", "source", "as_of", "formula"}:
            return node["value"]
        return {k: leaf_values(v) for k, v in node.items()}
    if isinstance(node, list):
        return [leaf_values(x) for x in node]
    return node


def build_context(template):
    """Compact plain-value view of everything gathered so far — this is what
    the LLM reasons over for SWOT/industry judgment, not the raw filing."""
    return {
        "company_profile": leaf_values(template["from_annual_report"]["company_profile"]),
        "financials": leaf_values(template["from_annual_report"]["financials"]),
        "derived": leaf_values(template["derived"]),
        "business_segments": leaf_values(template["from_annual_report"]["business_segments"]),
        "business_challenges": leaf_values(template["from_annual_report"]["business_challenges"]),
        "company_strategy": leaf_values(template["from_annual_report"]["company_strategy"]),
        "industry_narrative": leaf_values(template["from_annual_report"]["industry_narrative"]),
        "competitors": leaf_values(template["external_enrichment"]["competitors"]),
        "latest_news": leaf_values(template["external_enrichment"]["latest_news"]),
    }


def synthesize_swot(context):
    # Prior version did json.dumps(context)[:10000] — a flat character cutoff
    # on the WHOLE context dict. business_challenges (a major source of
    # threats: e.g. regulatory/antitrust risk) sits late in dict order and
    # was getting sliced off entirely before reaching the model. Truncate
    # each section independently instead, so every category gets *some*
    # representation no matter where it falls in the dict.
    PER_SECTION_CHARS = 3000
    trimmed = {
        k: (json.dumps(v, indent=2)[:PER_SECTION_CHARS] if v else v)
        for k, v in context.items()
    }
    data_block = "\n".join(f"## {k}\n{v}" for k, v in trimmed.items())

    prompt = f"""Based on this company data, produce a SWOT analysis.
Return ONLY valid JSON: {{"strengths": [...], "weaknesses": [...], "opportunities": [...], "threats": [...]}}
Each list item: {{"point": "...", "evidence": "..."}}. Evidence must cite a
specific fact from the data below — no generic statements. 3-5 points per category.
Pay particular attention to business_challenges for weaknesses/threats — do
not omit disclosed regulatory, legal, or antitrust risks even if they are a
single item in that section.

Data:
{data_block}

JSON:"""
    return call_llm_json_retry(prompt, "SWOT")


def synthesize_forecast(context):
    """Slide 27 needs an 'Industry Forecast' — no free structured source has
    this (confirmed: paid research firms only). Synthesized instead, same
    pattern as SWOT/industry_indicators: grounded in the company's own
    segment growth rates, strategy, and industry description (all real
    extracted facts), explicitly tagged as synthesized so it's never
    confused with a filing-sourced or third-party-research figure."""
    PER_SECTION_CHARS = 3000
    trimmed = {
        k: (json.dumps(v, indent=2)[:PER_SECTION_CHARS] if v else v)
        for k, v in context.items()
    }
    data_block = "\n".join(f"## {k}\n{v}" for k, v in trimmed.items())

    prompt = f"""Based on this company data, write a short (3-4 sentence)
industry forecast paragraph — near-term growth trajectory and key drivers,
grounded in the segment growth rates, strategy, and industry description
below. No invented statistics; only reason from what's given.
Return ONLY valid JSON: {{"forecast": "..."}}

Data:
{data_block}

JSON:"""
    result = call_llm_json_retry(prompt, "forecast")
    return result.get("forecast") if result else None


def synthesize_industry_indicators(context):
    PER_SECTION_CHARS = 3000
    trimmed = {
        k: (json.dumps(v, indent=2)[:PER_SECTION_CHARS] if v else v)
        for k, v in context.items()
    }
    data_block = "\n".join(f"## {k}\n{v}" for k, v in trimmed.items())

    prompt = f"""Based on this company/industry data, rate industry indicators.
Return ONLY valid JSON with these keys, each a short string judgment:
growth_rating, demand_trend, technology_adoption, spending_trend,
regulatory_environment, competitive_intensity, outlook.

Data:
{data_block}

JSON:"""
    return call_llm_json_retry(prompt, "industry_indicators")


def wrap_swot(swot_raw, source_note, as_of):
    if not swot_raw:
        return None
    wrapped = {}
    for category in ("strengths", "weaknesses", "opportunities", "threats"):
        items = swot_raw.get(category, [])
        wrapped[category] = [
            {
                "point": {"value": it.get("point"), "source": source_note, "as_of": as_of},
                "evidence": {"value": it.get("evidence"), "source": source_note, "as_of": as_of},
            }
            for it in items if isinstance(it, dict)
        ]
    return wrapped


def wrap_indicators(ind_raw, source_note, as_of):
    if not ind_raw:
        return None
    return {
        k: {"value": ind_raw.get(k), "source": source_note, "as_of": as_of}
        for k in (
            "growth_rating", "demand_trend", "technology_adoption",
            "spending_trend", "regulatory_environment",
            "competitive_intensity", "outlook",
        )
    }


def synthesize_industry_market_data(context):
    """No confirmed free structured API exists for market-size/forecast
    data (only paid research firms carry it) — but industry_indicators.
    growth_rating already proves LLM-judgment-from-filing-context is an
    acceptable substitute for this exact kind of forward-looking call.
    Same treatment here: forecast + growth_drivers, clearly labeled as
    synthesized, not scraped from a real market report."""
    PER_SECTION_CHARS = 3000
    trimmed = {
        k: (json.dumps(v, indent=2)[:PER_SECTION_CHARS] if v else v)
        for k, v in context.items()
    }
    data_block = "\n".join(f"## {k}\n{v}" for k, v in trimmed.items())

    prompt = f"""Based on this company/industry data, write a brief industry
forecast and list the main growth drivers for this company's industry.
Return ONLY valid JSON: {{"forecast": "2-3 sentence outlook", "growth_drivers": "comma-separated list of drivers"}}
Ground it in the data below — don't invent market-size figures or cite a
specific research firm; this is a judgment call, not a quoted statistic.

Data:
{data_block}

JSON:"""
    return call_llm_json_retry(prompt, "industry_market_data")


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python analysis.py TICKER YEAR")
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    path = os.path.join(EXTRACTED_DIR, ticker, year, "extracted.json")
    if not os.path.exists(path):
        raise SystemExit(f"Run full pipeline first — {path} missing")

    with open(path, encoding="utf-8") as f:
        template = json.load(f)

    context = build_context(template)
    as_of = datetime.now(timezone.utc).isoformat()
    source_note = "synthesized: LLM analysis over extracted + enriched data"

    swot_raw = synthesize_swot(context)
    wrapped_swot = wrap_swot(swot_raw, source_note, as_of)
    if wrapped_swot:
        template["analysis"]["swot"] = wrapped_swot

    ind_raw = synthesize_industry_indicators(context)
    wrapped_ind = wrap_indicators(ind_raw, source_note, as_of)
    if wrapped_ind:
        template["analysis"]["industry_indicators"] = wrapped_ind

    forecast_text = synthesize_forecast(context)
    if forecast_text:
        template["analysis"]["industry_forecast"] = {
            "value": forecast_text, "source": source_note, "as_of": as_of,
        }

    imd_raw = synthesize_industry_market_data(context)
    if imd_raw:
        imd = template["external_enrichment"]["industry_market_data"]
        if imd_raw.get("forecast"):
            imd["forecast"] = {"value": imd_raw["forecast"], "source": source_note, "as_of": as_of}
        if imd_raw.get("growth_drivers"):
            imd["growth_drivers"] = {"value": imd_raw["growth_drivers"], "source": source_note, "as_of": as_of}

    out_dir = os.path.join(FINAL_DIR, ticker, year)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "template.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"Final template saved: {out_path}")


if __name__ == "__main__":
    main()
