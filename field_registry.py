"""
field_registry.py — single source of truth: template field -> how it's filled.

Two extraction paths:
  1. XBRL_MAP   -> pulled straight from filing.xbrl() facts, no LLM.
  2. LLM_SECTION_MAP -> field belongs to a specific 10-K Item; LLM reads
     only that section (chunked), not the whole 300-400 page filing.

derive.py and analysis modules read DERIVED_MAP / ANALYSIS_FIELDS to know
what they own — extract_annual_report.py must NOT touch those.
"""

# ---------------------------------------------------------------------------
# 1. Direct XBRL lookup (us-gaap standard tags). No LLM, no ambiguity.
# ---------------------------------------------------------------------------
XBRL_MAP = {
    "from_annual_report.financials.financial_year": "DocumentFiscalYearFocus",
    "from_annual_report.financials.revenue.current_year": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "from_annual_report.financials.operating_income.current_year": "OperatingIncomeLoss",
    "from_annual_report.financials.net_income.current_year": "NetIncomeLoss",
    "from_annual_report.financials.gross_profit": "GrossProfit",
    "from_annual_report.financials.earnings_per_share": "EarningsPerShareDiluted",
    "from_annual_report.financials.operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
    "from_annual_report.financials.capital_expenditure": "PaymentsToAcquirePropertyPlantAndEquipment",
    "from_annual_report.financials.total_assets": "Assets",
    "from_annual_report.financials.total_liabilities": "Liabilities",
    "from_annual_report.financials.shareholders_equity": "StockholdersEquity",
    "from_annual_report.financials.cash_and_cash_equivalents": "CashAndCashEquivalentsAtCarryingValue",
    "from_annual_report.financials.total_debt": "LongTermDebtCurrent+LongTermDebtNoncurrent",
    "from_annual_report.financials.dividend": "CommonStockDividendsPerShareDeclared",
    "from_annual_report.financials.share_count": "CommonStockSharesOutstanding",
    "from_annual_report.financials.current_assets": "AssetsCurrent",
    "from_annual_report.financials.current_liabilities": "LiabilitiesCurrent",
    "from_annual_report.financials.depreciation_amortization": "DepreciationDepletionAndAmortization",
    "from_annual_report.financials.income_tax_expense": "IncomeTaxExpenseBenefit",
    # NOTE: employees is NOT a standard XBRL tag (confirmed absent in Apple's
    # facts) — moved to LLM_SECTION_MAP, extracted from Item 1 text instead.
}

# fallback tags — some companies/years use a different concept name for the
# same fact (e.g. older filings use plain "Revenues"). Extractor tries the
# primary tag first, then each fallback in order.
XBRL_FALLBACKS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ["Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    # NVIDIA doesn't tag cash capex under PaymentsToAcquirePropertyPlantAndEquipment
    # or PaymentsToAcquireProductiveAssets — confirmed BOTH absent in NVDA's raw
    # xbrl_facts.json (checked directly, not guessed). No fallback tag left here
    # on purpose: nvda:PaymentsForFinancedPropertyPlantAndEquipment... IS a real
    # tag but only covers a small *financed-lease* slice ($101M) — not the $6.1B
    # cash capex the filing text states. Using it as a fallback silently produces
    # a wrong-but-plausible-looking number, which is worse than null. Leave
    # capital_expenditure with NO fallback here; extract_annual_report.py's
    # capex proxy step (PP&E gross delta) fills it instead if this lookup misses.
    #
    # Confirmed (MSFT, this session): DepreciationDepletionAndAmortization is
    # ABSENT entirely from MSFT's xbrl_facts.json — checked directly, not
    # guessed. MSFT instead tags the cash-flow-statement combined line as
    # DepreciationAmortizationAndOther (verified real FY values: $38.5B /
    # $29.4B / $21.0B). Added as fallback. Note it includes "and other"
    # non-cash items alongside pure D&A, so it may run slightly high versus
    # a filer's separately-broken-out Depreciation + Amortization figures —
    # still the correct single-tag match for this field, not a guess.
    "DepreciationDepletionAndAmortization": ["DepreciationAmortizationAndOther"],
}

# Fields where no exact XBRL tag exists and value must be computed as a proxy,
# not looked up directly. derive.py should mark leaf.source with "(approximate)"
# so downstream consumers know this isn't a literal filed figure.
CAPEX_PROXY_NOTE = (
    "No direct cash capex XBRL tag present in this filer's facts; computed as "
    "PropertyPlantAndEquipmentGross(current) - PropertyPlantAndEquipmentGross(prior) "
    "as an approximation, cross-check against MD&A narrative capex figure if stated."
)

# historical_years[] built by re-running XBRL_MAP against prior fiscal years
# already present in the same filing (XBRL includes 2-3 yr comparatives).

# ---------------------------------------------------------------------------
# 2. LLM semantic extraction — mapped to 10-K Item number so we only feed
#    the relevant section to the model, never the full document.
# ---------------------------------------------------------------------------
LLM_SECTION_MAP = {
    # Item 1 - Business
    "from_annual_report.company_profile.company_name": "Item 1",
    # Item 2 - Properties. Confirmed (MSFT): HQ address ("Redmond, Washington")
    # and facility/office footprint sit here, NOT Item 1 — Item 1 never
    # mentions the HQ city/state at all (grep-verified). headquarters was
    # mismapped to Item 1 and came back null every run because of it, not
    # because the fact is missing from the filing.
    "from_annual_report.company_profile.headquarters": "Item 2",
    "from_annual_report.geographic_presence.offices": "Item 2",
    "from_annual_report.geographic_presence.countries": "Item 2",
    "from_annual_report.company_profile.employees": "Item 1",
    "from_annual_report.company_profile.brands": "Item 1",
    "from_annual_report.company_profile.services": "Item 1",
    "from_annual_report.company_profile.founded_year": "Item 1",
    # mission_vision_values, it_spending: NOT mapped here — location
    # varies by filer (confirmed: Item 1 doesn't work for every company).
    # Handled by fill_combined_search() in extract_annual_report.py, same
    # pattern as sustainability below.
    "from_annual_report.geographic_presence.regions": "Item 1",
    "from_annual_report.geographic_presence.delivery_centers": "Item 1",
    "from_annual_report.geographic_presence.geographic_revenue": "Item 1",
    "from_annual_report.geographic_presence.geographic_employee_distribution": "Item 1",
    "from_annual_report.business_segments.segments": "Item 1",
    "from_annual_report.industry_narrative": "Item 1",
    "from_annual_report.acquisitions.acquisitions": "Item 1",
    "from_annual_report.partnerships.partnerships": "Item 1",
    "from_annual_report.technology": "Item 1",

    # Item 1A - Risk Factors
    "from_annual_report.business_challenges.challenges": "Item 1A",

    # Item 7 - MD&A
    # company_strategy: NOT mapped here — see mission_vision_values note
    # above, same fragile-single-Item problem (confirmed: MSFT has it in
    # Item 7, Apple/Alphabet have it in Item 1). Handled by
    # fill_combined_search().
    "from_annual_report.financials.historical_years": "Item 7",  # narrative commentary only, numbers via XBRL

    # Item 8 - Financial Statements (segment revenue split covered by derived.segment_revenue_percentage)

    # Proxy-adjacent / Item 10-12 (sometimes incorporated by reference — may be absent from 10-K itself)
    "from_annual_report.organization.organization_structure": "Item 1",
    # organization.people / board_of_directors are list fields — handled by
    # extract_lists.py's LIST_FIELDS (mapped to "Signatures" section), not here.

    # Sustainability — NOT mapped here on purpose. Location varies by filer
    # (confirmed: MSFT has it in Item 1, Alphabet in Item 1A/7, neither in
    # Item 1 alone) — handled by fill_sustainability() in
    # extract_annual_report.py, which searches Item 1+1A+7 combined instead
    # of committing to one fixed Item.
}

# ---------------------------------------------------------------------------
# 3. Pure math — derive.py computes these from from_annual_report values.
#    Never sent to LLM, never fetched externally.
# ---------------------------------------------------------------------------
DERIVED_MAP = {
    "derived.revenue_growth": "financials.revenue.current_year vs previous_year",
    "derived.operating_income_growth": "financials.operating_income.current_year vs previous_year",
    "derived.net_income_growth": "financials.net_income.current_year vs previous_year",
    "derived.operating_margin": "financials.operating_income.current_year / financials.revenue.current_year",
    "derived.net_margin": "financials.net_income.current_year / financials.revenue.current_year",
    "derived.gross_margin": "financials.gross_profit / financials.revenue.current_year",
    "derived.free_cash_flow": "financials.operating_cash_flow - financials.capital_expenditure",
    "derived.net_debt": "financials.total_debt - financials.cash_and_cash_equivalents",
    "derived.roe": "financials.net_income.current_year / financials.shareholders_equity",
    "derived.roa": "financials.net_income.current_year / financials.total_assets",
    "derived.debt_to_equity": "financials.total_debt / financials.shareholders_equity",
    "derived.current_ratio": "financials.current_assets / financials.current_liabilities",
    "derived.segment_revenue_percentage": "per segment: segment.revenue / financials.revenue.current_year",
    "derived.ebit": "financials.operating_income.current_year (operating income = EBIT)",
    "derived.ebitda": "financials.operating_income.current_year + financials.depreciation_amortization",
    "derived.profit_before_tax": "financials.net_income.current_year + financials.income_tax_expense",
    "derived.quarterly_revenue_growth": "yoy % change on quarterly revenue",
    "derived.quarterly_net_income_growth": "yoy % change on quarterly net income",
    # not derived.* leaf — alias written directly onto
    # from_annual_report.company_profile.revenue by derive.py, same value as
    # financials.revenue.current_year. Documented here so it isn't
    # rediscovered as a "missing fetch" later.
    "company_profile.revenue (alias)": "financials.revenue.current_year",
}

# ---------------------------------------------------------------------------
# 4. LLM synthesis — needs from_annual_report + external data already filled.
#    Runs last, after extraction + enrichment stages complete.
# ---------------------------------------------------------------------------
ANALYSIS_FIELDS = [
    "analysis.swot",
    "analysis.industry_indicators",
    "external_enrichment.industry_market_data",
]

# ---------------------------------------------------------------------------
# 5. Not in annual report at all — external_enrichment module owns these.
#    extract_annual_report.py must skip entirely.
# ---------------------------------------------------------------------------
EXTERNAL_ONLY = [
    "external_enrichment.investor_information",
    "external_enrichment.latest_news",
    "external_enrichment.competitors",
    "external_enrichment.awards_and_accolades",
    "external_enrichment.deals",
]
# leadership photo/linkedin now filled directly onto
# from_annual_report.organization.people[] (and .technology_team[]) by
# enrich.py — no longer a separate external_enrichment.leadership_links block.
# vendors_and_contracts merged into external_enrichment.deals (single list,
# vendor/start_date/end_date/contract_details).
# esg_ratings removed entirely — not on any PPT slide, no free source either.
# industry_market_data moved to ANALYSIS_FIELDS below — no free structured
# API for forecast/growth-drivers text was ever found (confirmed across
# multiple sessions), but analysis.py already proves LLM-synthesis-from-
# filing-context works fine for this exact kind of judgment call (that's
# how industry_indicators.growth_rating gets filled) — same treatment.

# ---------------------------------------------------------------------------
# 6. Source reliability for external_enrichment fields.
#    Not a stored "credibility score" field on the leaf — the leaf's
#    `source` URL IS the credibility signal, same as everywhere else in
#    this pipeline. What changes: search-result SELECTION now prefers an
#    authoritative domain over whatever ranked first in raw search output.
#    Implemented in enrich.py as DOMAIN_TIERS + pick_best(); listed here so
#    the tier list has one documented home instead of living silently
#    inside enrich.py only.
#      esg_ratings          -> rating agencies (msci.com, sustainalytics.com,
#                               spglobal.com) over ESG-aggregator blogs
#      industry_market_data -> research firms (statista.com, gartner.com,
#                               idc.com, mckinsey.com) over SEO content farms
#      latest_news / awards -> wire services + major outlets (reuters,
#                               bloomberg, apnews, businesswire, prnewswire)
#                               over generic blogs or the company's own
#                               marketing hub pages
#      investor_information -> yfinance primary; stooq.com CSV fallback for
#                               price only if yfinance is unavailable
#      competitors           -> filing text itself (Item 1/1A) for names;
#                               revenue/employees/market_cap filled via
#                               SEC ticker lookup + yfinance if the named
#                               competitor is publicly traded
#      leadership (people[]) -> Wikipedia REST summary API (photo+bio);
#                               LinkedIn filled as a search-style link only
#                               (no free API returns a verified profile URL)
#      deals                  -> USAspending.gov (US federal contracts only)
#
# DDG/ddgs REMOVED entirely (rate-limit blocking). latest_news / awards now
# use Google News RSS (news.google.com/rss/search) — free, no key, not a
# scraper. esg_ratings / industry_market_data have no free-API replacement —
# left null, manual/paid-source only.
# ---------------------------------------------------------------------------
