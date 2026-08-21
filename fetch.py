"""
fetch.py — US annual report fetcher (SEC EDGAR via edgartools)
Stage 1 of pipeline: company -> confirm -> CIK -> 10-K -> save raw files.

Install: pip install edgartools --break-system-packages

Usage:
    python fetch.py "Apple"
    python fetch.py "Apple" --year 2024
"""

import argparse
import json
import os
from datetime import datetime, timezone

from edgar import set_identity, find_company, Company

IDENTITY = "CompanyIntel ihavenoenemigos@gmail.com"
RAW_DIR = "raw/US"


import difflib
import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def search_company(name: str, limit: int = 5):
    """Fetch SEC's company list directly, match on ticker or name.

    Previous version only ever fuzzy-matched against company NAME, so typing
    a ticker (e.g. "amzn") never matched anything real — and difflib's
    cutoff=0.3 is loose enough that it returns near-random noise ("Sanofi"
    for "netflix") instead of falling through to the substring fallback,
    since 0.3 rarely returns zero matches for ANY input.

    New order, each tried only if the previous stage found nothing:
      1. exact ticker match (case-insensitive) — "amzn" -> AMZN directly
      2. substring match on company name — reliable, no false positives
      3. fuzzy match on name, cutoff raised to 0.6 — last resort only
    """
    headers = {"User-Agent": IDENTITY}
    resp = requests.get(TICKERS_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()  # {"0": {"cik_str":..., "ticker":..., "title":...}, ...}

    rows = list(data.values())
    name_lower = name.lower().strip()

    # 1. exact ticker match — covers "amzn", "AMZN", "Amzn" etc.
    ticker_matches = [r for r in rows if r["ticker"].lower() == name_lower]
    if ticker_matches:
        return ticker_matches[:limit]

    # 2. substring match on company name — covers "amazon", "google" (won't
    # find Alphabet under this name, that's a real ambiguity, not a bug —
    # user should type "Alphabet" or the ticker "GOOGL")
    substring_matches = [r for r in rows if name_lower in r["title"].lower()]
    if substring_matches:
        return substring_matches[:limit]

    # 3. fuzzy match as last resort, only if nothing above matched at all.
    # cutoff raised from 0.3 (near-random) to 0.6 (real similarity required).
    names = [row["title"] for row in rows]
    fuzzy_matches = difflib.get_close_matches(name, names, n=limit, cutoff=0.6)
    if fuzzy_matches:
        return [r for r in rows if r["title"] in fuzzy_matches][:limit]

    raise SystemExit(f"No company found for '{name}'")
def fetch_10k(cik: int, ticker: str, year: int | None, form: str = "10-K"):
    company = Company(cik)
    filings = company.get_filings(form=form)
    if form == "10-Q":
        # quarterly: no --year match needed, always want the latest
        filing = filings.latest()
    elif year:
        filing = next(
            (f for f in filings if f.filing_date.year == year), None
        )
        if not filing:
            raise SystemExit(f"No {form} found for FY{year}.")
    else:
        filing = filings.latest()

    filing_year = str(filing.filing_date.year)
    if form == "10-Q":
        # Confirmed bug: using the 10-Q's OWN filing_date.year put quarterly
        # data in a different year-folder than the annual data lives in
        # whenever the two filings' calendar years differ (Apple: 10-K
        # filed in 2025, 10-Q filed in 2026 — quarterly.py looks for
        # raw/US/AAPL/2025/quarterly/, but this wrote to .../2026/quarterly/,
        # so quarterly.py silently found nothing). Use the annual `year`
        # the caller already resolved instead, so both stages agree on
        # which folder holds this company's data.
        out_dir = os.path.join(RAW_DIR, ticker.upper(), str(year) if year else filing_year, "quarterly")
    else:
        out_dir = os.path.join(RAW_DIR, ticker.upper(), filing_year)
    os.makedirs(out_dir, exist_ok=True)

    # save filing HTML
    html_path = os.path.join(out_dir, "filing.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(filing.html())

    # save XBRL financial facts (structured, skip LLM for these)
    xbrl_path = os.path.join(out_dir, "xbrl_facts.json")
    try:
        xbrl = filing.xbrl()
        facts = xbrl.facts.to_dataframe() if xbrl else None
        if facts is not None:
            facts.to_json(xbrl_path, orient="records", indent=2)
    except Exception as e:
        print(f"[warn] XBRL extraction failed: {e}")

    # save metadata
    metadata = {
        "company": company.name,
        "cik": cik,
        "ticker": ticker.upper(),
        "form": form,
        "filing_date": filing.filing_date.isoformat(),
        "period_of_report": getattr(filing, "period_of_report", None),
        "accession_no": filing.accession_no,
        "source_url": filing.filing_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = os.path.join(out_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"\nSaved:\n  {html_path}\n  {xbrl_path}\n  {meta_path}")
    return out_dir

def pick_company(candidates):
    print("\nTop matches:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c['title']}  (CIK {c['cik_str']}, ticker: {c['ticker']})")
    choice = input("\nPick number (or 'c' to cancel): ").strip()
    if choice.lower() == "c":
        raise SystemExit("Cancelled.")
    idx = int(choice) - 1
    if not (0 <= idx < len(candidates)):
        raise SystemExit("Invalid choice.")
    return candidates[idx]





def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("company_name")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--form", choices=["10-K", "10-Q"], default="10-K")
    parser.add_argument("--cik", type=int, default=None, help="skip interactive search, e.g. re-fetch 10-Q for a known company")
    parser.add_argument("--ticker", default=None)
    args = parser.parse_args()

    set_identity(IDENTITY)

    if args.cik and args.ticker:
        cik, ticker = args.cik, args.ticker
    else:
        candidates = search_company(args.company_name)
        company = pick_company(candidates)
        cik = int(company["cik_str"])
        ticker = company["ticker"]

    fetch_10k(cik, ticker, args.year, form=args.form)


if __name__ == "__main__":
    main()
