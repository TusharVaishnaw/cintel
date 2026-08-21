#!/usr/bin/env python3
"""
extract_slide_data.py <company.json> <out slide_data.json>

Unwraps the {value, source, as_of} leaf pattern from the pipeline's output
JSON into plain values, drops nulls/empties, and pre-computes the bits the
renderer shouldn't have to think about (chart series, % splits, groupings).

Never fabricates content: a missing value stays absent from slide_data.json,
and the renderer skips that row/card/slide element entirely rather than
printing a placeholder.
"""
import sys, json


def unwrap(o):
    """Recursively strip {value, source/formula, as_of} leaves down to just value.
    Two leaf shapes exist in this schema: {value,source,as_of} for extracted
    facts, {value,formula,as_of} for derived/computed fields (segment
    revenue_percentage, everything in derived.*). Matching only the first
    shape silently left every derived.* field as an un-unwrapped dict, which
    then failed num()/fmt_pct() and rendered as None everywhere downstream."""
    if isinstance(o, dict):
        if "value" in o and set(o.keys()) <= {"value", "source", "formula", "as_of"}:
            return unwrap(o["value"])
        return {k: unwrap(v) for k, v in o.items()}
    if isinstance(o, list):
        return [unwrap(x) for x in o]
    return o


def s(x):
    """Trim, treat empty string as missing. Cleans float-ified year strings ('2025.0' -> '2025')."""
    if x is None:
        return None
    x = str(x).strip()
    if x.endswith(".0") and x[:-2].isdigit():
        x = x[:-2]
    return x if x else None


def num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_money(v):
    if v is None:
        return None
    v = float(v)
    for div, suf in [(1e12, "T"), (1e9, "B"), (1e6, "M")]:
        if abs(v) >= div:
            return f"${v/div:.1f}{suf}"
    return f"${v:,.0f}"


def fmt_pct(v):
    """For fields already on a percent scale (e.g. derive.py's pct_growth)."""
    if v is None:
        return None
    return f"{v:+.1f}%"


def fmt_ratio_pct(v):
    """For fields on a 0-1 fraction scale (e.g. derive.py's ratio()) — margins,
    ROE/ROA. Scales by 100 before formatting. Using fmt_pct on these silently
    printed 0.348 as '+0.3%' instead of '+34.8%' — confirmed against derive.py."""
    if v is None:
        return None
    return f"{v * 100:+.1f}%"


def drop_empty(lst):
    """Drop list rows whose dict entries are all None, and prune None fields."""
    out = []
    for row in lst or []:
        if not isinstance(row, dict):
            if row:
                out.append(row)
            continue
        cleaned = {k: v for k, v in row.items() if v not in (None, "", [], {})}
        if cleaned:
            out.append(cleaned)
    return out


def main():
    if len(sys.argv) != 3:
        print("usage: extract_slide_data.py <company.json> <out.json>", file=sys.stderr)
        sys.exit(1)

    raw = json.load(open(sys.argv[1]))
    d = unwrap(raw)

    meta = d.get("metadata", {})
    ar = d.get("from_annual_report", {})
    qr = d.get("from_quarterly_report", {})
    der = d.get("derived", {})
    ana = d.get("analysis", {})
    ext = d.get("external_enrichment", {})

    cp = ar.get("company_profile", {})
    fin = ar.get("financials", {})
    inv = ext.get("investor_information", {})

    out = {}

    out["company_name"] = s(meta.get("company")) or s(cp.get("company_name")) or "Company"
    out["ticker"] = s(meta.get("ticker"))
    out["report_year"] = s(meta.get("annual_report_year"))

    # --- Slide 2: Company overview ---
    out["overview"] = {
        "founded": s(cp.get("founded_year")),
        "headquarters": s(cp.get("headquarters")),
        "employees": s(cp.get("employees")),
        "revenue": fmt_money(num(cp.get("revenue"))) or s(cp.get("revenue")),
        "market_cap": fmt_money(num(inv.get("market_cap"))),
        "brief": s(cp.get("services")) and None,  # no single "brief" field in schema; build below
        "brands": s(cp.get("brands")),
        "services": s(cp.get("services")),
        "key_competitors": [s(c.get("company_name")) for c in ext.get("competitors", {}).get("key_competitors", [])
                             if s(c.get("company_name"))][:5],
        "key_acquisitions": [f"{s(a.get('year')) or ''} {s(a.get('company_name')) or ''}".strip()
                              for a in ar.get("acquisitions", {}).get("acquisitions", [])
                              if s(a.get("company_name"))][:5],
    }

    # --- Slide 3: Mission / Vision / Values ---
    mvv = ar.get("mission_vision_values", {})
    out["mission_vision"] = {
        "mission": s(mvv.get("mission")),
        "vision": s(mvv.get("vision")),
        "values": s(mvv.get("values")),
    }

    # --- Slide 4: Geo presence ---
    geo = ar.get("geographic_presence", {})
    out["geo"] = {
        "countries": s(geo.get("countries")),
        "regions": s(geo.get("regions")),
        "offices": s(geo.get("offices")),
        "delivery_centers": s(geo.get("delivery_centers")),
        "geographic_revenue": s(geo.get("geographic_revenue")),
    }

    # --- Slide 5: Business segments ---
    segs = drop_empty(ar.get("business_segments", {}).get("segments", []))
    seg_rows = []
    for seg in segs:
        seg_rows.append({
            "name": s(seg.get("segment_name")),
            "revenue": fmt_money(num(seg.get("revenue"))),
            "revenue_pct": num(seg.get("revenue_percentage")),
            "growth": fmt_pct(num(seg.get("growth_rate"))),
            "op_margin": fmt_ratio_pct(num(seg.get("operating_margin"))),
            "description": s(seg.get("description")),
        })
    out["segments"] = seg_rows

    # --- Slide 6/7: Sustainability ---
    sus = ar.get("sustainability", {})
    out["sustainability"] = {
        "strategy": s(sus.get("strategy")),
        "goals": s(sus.get("goals")),
        "key_initiatives": s(sus.get("key_initiatives")),
    }

    # --- Slide 8: Org structure ---
    out["org_structure"] = s(ar.get("organization", {}).get("organization_structure"))

    # --- Slide 9: Leadership ---
    people = drop_empty(ar.get("organization", {}).get("people", []))
    out["leadership"] = [{
        "name": s(p.get("name")),
        "designation": s(p.get("designation")),
        "brief": s(p.get("brief")),
        "linkedin_url": s(p.get("linkedin_url")),
        "photo_url": s(p.get("photo_url")),
    } for p in people if s(p.get("name"))]

    tech_team = drop_empty(ar.get("organization", {}).get("technology_team", []))
    out["technology_team"] = [{
        "name": s(p.get("name")),
        "designation": s(p.get("designation")),
        "brief": s(p.get("brief")),
        "linkedin_url": s(p.get("linkedin_url")),
        "photo_url": s(p.get("photo_url")),
    } for p in tech_team if s(p.get("name"))]

    # --- Slide 10-14: SWOT ---
    swot = ana.get("swot", {})
    out["swot"] = {}
    for k in ("strengths", "weaknesses", "opportunities", "threats"):
        rows = drop_empty(swot.get(k, []))
        out["swot"][k] = [{
            "point": s(r.get("point")),
            "evidence": s(r.get("evidence")),
            "detail": s(r.get("detail")),
        } for r in rows if s(r.get("point"))]

    # --- Slide 15: Financials annual (3yr chart) ---
    def yr_triplet(node):
        return {
            "current": num(node.get("current_year")),
            "previous": num(node.get("previous_year")),
            "two_prior": num(node.get("two_years_prior")),
        }
    rev3 = yr_triplet(fin.get("revenue", {}))
    oi3 = yr_triplet(fin.get("operating_income", {}))
    ni3 = yr_triplet(fin.get("net_income", {}))
    years = [s(fin.get("financial_year"))]  # only current year label reliably known
    out["financials_annual"] = {
        "revenue": rev3, "operating_income": oi3, "net_income": ni3,
        "financial_year": years[0],
        "highlights": {
            "ebitda": fmt_money(num(fin.get("ebitda"))),
            "gross_profit": fmt_money(num(fin.get("gross_profit"))),
            "eps": fin.get("earnings_per_share"),
            "revenue_growth": fmt_pct(num(der.get("revenue_growth"))),
            "operating_margin": fmt_ratio_pct(num(der.get("operating_margin"))),
            "net_margin": fmt_ratio_pct(num(der.get("net_margin"))),
            "free_cash_flow": fmt_money(num(der.get("free_cash_flow"))),
            "roe": fmt_ratio_pct(num(der.get("roe"))),
        },
        "historical_years": drop_empty(fin.get("historical_years", [])),
    }

    # --- Slide 16: Quarterly ---
    out["financials_quarterly"] = {
        "quarter": s(qr.get("quarter")),
        "revenue": fmt_money(num(qr.get("revenue"))),
        "operating_income": fmt_money(num(qr.get("operating_income"))),
        "net_income": fmt_money(num(qr.get("net_income"))),
        "ebitda": fmt_money(num(qr.get("ebitda"))),
        "eps": qr.get("eps"),
        "cash_flow": fmt_money(num(qr.get("cash_flow"))),
        "revenue_growth": fmt_pct(num(der.get("quarterly_revenue_growth"))),
        "net_income_growth": fmt_pct(num(der.get("quarterly_net_income_growth"))),
    }

    # --- Slide 17: Acquisitions ---
    out["acquisitions"] = [{
        "year": s(a.get("year")), "company_name": s(a.get("company_name")),
        "brief": s(a.get("brief")), "value": fmt_money(num(a.get("acquisition_value"))) or s(a.get("acquisition_value")),
    } for a in drop_empty(ar.get("acquisitions", {}).get("acquisitions", []))
        if s(a.get("company_name"))]

    # --- Slide 18: Competitors ---
    out["competitors"] = [{
        "name": s(c.get("company_name")),
        "revenue": fmt_money(num(c.get("revenue"))),
        "employees": s(c.get("employees")),
        "market_cap": fmt_money(num(c.get("market_cap"))),
        "ict_budget": s(c.get("ict_budget")),
    } for c in drop_empty(ext.get("competitors", {}).get("key_competitors", []))
        if s(c.get("company_name"))]

    # --- Slide 19: Awards ---
    out["awards"] = [{
        "date": s(a.get("date")), "award": s(a.get("award")), "brief": s(a.get("brief")),
    } for a in drop_empty(ext.get("awards_and_accolades", {}).get("awards", []))
        if s(a.get("award"))]

    # --- Slide 20: Business challenges ---
    out["challenges"] = [{
        "challenge": s(c.get("challenge")), "impact": s(c.get("impact")), "brief": s(c.get("brief")),
    } for c in drop_empty(ar.get("business_challenges", {}).get("challenges", []))
        if s(c.get("challenge"))]

    # --- Slide 21: Latest news, grouped by category (bulletin) ---
    news_raw = drop_empty(ext.get("latest_news", {}).get("news", []))
    groups = {"Partnership": [], "Products/Technology": [], "Recognition": []}
    for n in news_raw:
        cat = s(n.get("category")) or "Products/Technology"
        # normalize loose category strings from the LLM
        cat_l = cat.lower()
        if "partner" in cat_l:
            key = "Partnership"
        elif "recogn" in cat_l or "certif" in cat_l or "award" in cat_l:
            key = "Recognition"
        else:
            key = "Products/Technology"
        groups[key].append({
            "date": s(n.get("date")), "title": s(n.get("title")),
            "summary": s(n.get("summary")), "url": s(n.get("url")),
        })
    for k in groups:
        groups[k] = groups[k][:4]  # cap per column so the slide doesn't overflow
    out["news"] = groups

    # --- Slide 22: IT spending ---
    out["it_spending"] = s(ar.get("it_spending", {}).get("brief"))

    # --- Slide 23: Deals ---
    out["deals"] = [{
        "vendor": s(dl.get("vendor")), "start_date": s(dl.get("start_date")),
        "end_date": s(dl.get("end_date")), "contract_details": s(dl.get("contract_details")),
    } for dl in drop_empty(ext.get("deals", {}).get("deals", []))
        if s(dl.get("vendor"))][:12]  # cap so one slide stays legible

    # --- Slide 24: Technology initiatives ---
    out["tech_initiatives"] = [{
        "date": s(t.get("date")), "title": s(t.get("title")), "details": s(t.get("details")),
    } for t in drop_empty(ar.get("technology", {}).get("technology_initiatives", []))
        if s(t.get("title"))]

    # --- Slide 25: Technologies in use ---
    out["technologies_in_use"] = [{
        "technology": s(t.get("technology")), "category": s(t.get("category")), "brief": s(t.get("brief")),
    } for t in drop_empty(ar.get("technology", {}).get("technologies_in_use", []))
        if s(t.get("technology"))]

    # --- Slide 26: Industry indicators ---
    ind = ana.get("industry_indicators", {})
    out["industry_indicators"] = {
        "growth_rating": s(ind.get("growth_rating")),
        "demand_trend": s(ind.get("demand_trend")),
        "technology_adoption": s(ind.get("technology_adoption")),
        "spending_trend": s(ind.get("spending_trend")),
        "regulatory_environment": s(ind.get("regulatory_environment")),
        "competitive_intensity": s(ind.get("competitive_intensity")),
        "outlook": s(ind.get("outlook")),
    }

    # --- Slide 27: Industry forecast + competitive landscape ---
    narr = ar.get("industry_narrative", {})
    out["industry_forecast"] = {
        "industry": s(narr.get("industry")),
        "market_size": s(narr.get("market_size")),
        "market_size_year": s(narr.get("market_size_year")),
        "competitive_landscape": s(narr.get("competitive_landscape_description")),
        "forecast": s(ana.get("industry_forecast")),
        "growth_drivers": s(ext.get("industry_market_data", {}).get("growth_drivers")),
    }

    json.dump(out, open(sys.argv[2], "w"), indent=1)
    print(f"wrote {sys.argv[2]}")


if __name__ == "__main__":
    main()
