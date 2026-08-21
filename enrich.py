"""
enrich.py — Stage 6: fills external_enrichment.* + leadership photo/linkedin
onto organization.people[] / organization.technology_team[], from real
external sources. No LLM synthesis, no inference — real source or null.

DDG REMOVED ENTIRELY (was rate-limit blocking every run). Replacements:
  investor_information  -> yfinance (unchanged)
  latest_news            -> Google News RSS (news.google.com/rss/search),
                             free, no key, not a scraper — category assigned
                             by keyword heuristic (Partnership /
                             Products/Technology / Recognition)
  awards_and_accolades   -> Google News RSS, same source, title-filtered
  competitors             -> LLM names them from filing text (Item 1/1A);
                             revenue/employees/market_cap filled via SEC
                             ticker lookup + yfinance IF the named
                             competitor is public. ict_budget has no free
                             source anywhere — always null.
  people[]/technology_team[] leadership details ->
                             Wikipedia REST summary API (photo + bio) per
                             name already extracted; linkedin_url is a
                             constructed SEARCH link only, not a verified
                             profile match (flag as manual-check)
  deals                   -> USAspending.gov (US federal contracts the
                             company holds as vendor). US-govt-business
                             companies only — null otherwise, correctly.

Deliberately left null, no free source identified:
  esg_ratings            -> raw ratings paywalled, no reliable free API
  industry_market_data   -> paid research firm data, not confirmed repeatable

Install:
    pip install yfinance requests --break-system-packages

Usage:
    python enrich.py AAPL 2025
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from extract_annual_report import split_items, call_llm, max_chars_for
from fetch import search_company

EXTRACTED_DIR = "extracted/US"
RAW_DIR = "raw/US"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def leaf(value, source, as_of=None):
    return {"value": value, "source": source, "as_of": as_of or now_iso()}


_LEGAL_SUFFIXES = re.compile(
    r"\b(CORP(ORATION)?|INC|CO|LTD|LLC|PLC|L\.?P\.?|COMPANY)\.?$",
    re.IGNORECASE,
)


def clean_company_name(name):
    if not name:
        return name
    cleaned = _LEGAL_SUFFIXES.sub("", name).strip().rstrip(",")
    if cleaned.isupper():
        cleaned = cleaned.title()
    return cleaned or name


# ---------------------------------------------------------------------------
# investor_information — yfinance, unchanged
# ---------------------------------------------------------------------------
def _yfinance_info(ticker):
    import yfinance as yf

    for attempt in range(3):
        try:
            t = yf.Ticker(ticker)
            info = t.info
            if info and (info.get("regularMarketPrice") is not None or info.get("currentPrice") is not None):
                return info, "yfinance:info"
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate limited" in msg:
                wait = 5 * (attempt + 1) + (attempt * 0.5)
                print(f"[warn] yfinance rate-limited, retrying in {wait:.1f}s ({attempt + 1}/3)")
                time.sleep(wait)
                continue
            break
    try:
        fi = yf.Ticker(ticker).fast_info
        if fi and fi.get("lastPrice") is not None:
            return {
                "marketCap": fi.get("marketCap"),
                "currentPrice": fi.get("lastPrice"),
                "sharesOutstanding": fi.get("shares"),
                "exchange": fi.get("exchange"),
            }, "yfinance:fast_info"
    except Exception as e:
        print(f"[warn] yfinance fast_info also failed: {e}")
    return None, None


def _stooq_fallback(ticker):
    url = f"https://stooq.com/q/l/?s={ticker.lower()}.us&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None, None
        row = dict(zip(lines[0].split(","), lines[1].split(",")))
        close = row.get("Close")
        if not close or close == "N/D":
            return None, None
        return {"currentPrice": float(close)}, url
    except Exception as e:
        print(f"[warn] stooq fallback failed: {e}")
        return None, None


def fill_investor_information(template, ticker):
    try:
        import yfinance  # noqa: F401
    except ImportError:
        print("[warn] yfinance not installed, skipping investor_information")
        return
    info, src_tag = _yfinance_info(ticker)
    inv = template["external_enrichment"]["investor_information"]
    if info:
        src = f"{src_tag}: {ticker}"
        inv["market_cap"] = leaf(info.get("marketCap"), src)
        inv["share_price"] = leaf(info.get("currentPrice") or info.get("regularMarketPrice"), src)
        inv["shares_outstanding"] = leaf(info.get("sharesOutstanding"), src)
        inv["stock_exchange"] = leaf(info.get("exchange"), src)
        inv["ticker"] = leaf(ticker, src)
        print(f"  investor_information: filled from {src_tag}")
        return
    stooq_data, stooq_url = _stooq_fallback(ticker)
    if stooq_data:
        inv["share_price"] = leaf(stooq_data["currentPrice"], stooq_url)
        inv["ticker"] = leaf(ticker, stooq_url)
        print("  investor_information: yfinance unavailable, share_price from stooq")
    else:
        print("  investor_information: yfinance and stooq both unavailable, skipping")


# ---------------------------------------------------------------------------
# News source — NewsAPI.org (licensed, commercial-safe at scale). Requires
# NEWSAPI_KEY env var; https://newsapi.org/pricing — the free Developer tier
# is dev/test only (no commercial use, 100 req/day, results delayed 24h).
# For 100-500 companies run repeatedly over time, use a paid Business plan.
#
# Google News RSS (news.google.com/rss/search) is kept ONLY as a fallback
# when NEWSAPI_KEY is unset, clearly flagged each run — it's an unofficial
# consumer feed, not a licensed API, and Google's ToS does not authorize
# automated bulk querying. Fine for one-off dev testing; NOT fine for
# repeated commercial use across a large company list.
# ---------------------------------------------------------------------------
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY")

# SEC legal name often isn't how the company is discussed in press.
# Confirmed bug: GOOGL's legal name "Alphabet Inc." -> cleaned "Alphabet",
# but most coverage says "Google" -> quoted-phrase filter excluded
# legitimate Google-branded articles, latest_news dropped 44->3. Add
# aliases here as discovered; ticker is the key (stable across renames).
COMPANY_ALIASES = {
    "GOOGL": ["Google", "Alphabet"],
    "GOOG": ["Google", "Alphabet"],
    "META": ["Facebook", "Meta"],
    "FB": ["Facebook", "Meta"],
}


def _aliases_for(ticker, cleaned_name):
    names = COMPANY_ALIASES.get((ticker or "").upper())
    return names if names else [cleaned_name]


def _alias_query_clause(aliases):
    """Quoted-OR clause for NewsAPI/RSS: "Google" OR "Alphabet" """
    return "(" + " OR ".join(f'"{a}"' for a in aliases) + ")"


def newsapi_search(query, max_results=10):
    if not NEWSAPI_KEY:
        return None  # caller falls back to RSS with a warning
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query, "language": "en", "sortBy": "publishedAt",
        "pageSize": max_results, "apiKey": NEWSAPI_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=15, verify=False)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            print(f"[warn] NewsAPI error: {data.get('message')}")
            return []
        items = []
        for a in data.get("articles", []):
            items.append({
                "title": a.get("title"), "url": a.get("url"),
                "date": a.get("publishedAt"),
                "source": (a.get("source") or {}).get("name"),
                "summary": a.get("description"),
            })
        return items
    except Exception as e:
        print(f"[warn] NewsAPI request failed for '{query}': {e}")
        return []


def google_news_rss(query, max_results=8):
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=15, verify=False)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:max_results]:
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub_date = item.findtext("pubDate") or ""
            source_el = item.find("source")
            source_name = source_el.text if source_el is not None else None
            desc = item.findtext("description") or ""
            items.append({
                "title": title, "url": link, "date": pub_date,
                "source": source_name, "summary": re.sub(r"<[^>]+>", "", desc).strip(),
            })
        return items
    except Exception as e:
        print(f"[warn] Google News RSS failed for '{query}': {e}")
        return []


def news_search(query, max_results=8):
    """Single entry point both fill_latest_news/fill_awards call — routes to
    NewsAPI if licensed key present, RSS fallback otherwise (with warning)."""
    result = newsapi_search(query, max_results)
    if result is not None:
        return result
    print(f"[warn] NEWSAPI_KEY not set — using unlicensed Google News RSS fallback "
          f"for '{query}'. Fine for dev/testing; get a NewsAPI.org key before "
          f"running this at commercial scale (100+ companies, repeated over time).")
    return google_news_rss(query, max_results)


NEWS_CATEGORY_KEYWORDS = {
    "Partnership": ["partner", "partnership", "collaborat", "alliance", "joint venture"],
    "Products/Technology": ["launch", "unveil", "release", "product", "platform", "technology", "ai ", "acquir"],
    "Recognition": ["award", "recogni", "named", "ranked", "top ", "best "],
}


def categorize_news(title, summary):
    text = f"{title} {summary}".lower()
    for category, keywords in NEWS_CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None


_ADULT_CONTENT_DOMAINS_KEYWORDS = (
    "porn", "xxx", "fuck", "sex", "nsfw", "adult", "camgirl", "onlyfans",
)


def _is_relevant_news(title, summary, url, aliases):
    """Confirmed bug: fill_latest_news() had zero subject filter — pulled
    raw hits for a bare company-name query with no relevance check,
    surfacing unrelated celebrity gossip, deal listings, and (for "Apple")
    outright adult content, since NewsAPI's query does loose relevance
    ranking, not phrase matching (same root cause as the awards bug fixed
    earlier). Require ANY known alias literally appear in title or
    summary (not just the SEC legal name — "Google" for GOOGL), and
    hard-block adult-content domains/titles regardless of match."""
    text = f"{title} {summary} {url}".lower()
    if any(kw in text for kw in _ADULT_CONTENT_DOMAINS_KEYWORDS):
        return False
    combined_title_summary = f"{title} {summary}".lower()
    return any(a.lower() in combined_title_summary for a in aliases)


def fill_latest_news(template, company_name, ticker=None):
    q = clean_company_name(company_name)
    aliases = _aliases_for(ticker, q)
    # quoted-OR across aliases — same fix as awards: NewsAPI's q does loose
    # relevance matching on unquoted text, not literal phrase search
    results = news_search(_alias_query_clause(aliases), max_results=15)
    rows = []
    for r in results:
        if not r.get("url") or not r.get("title"):
            continue
        title, summary = r["title"], r.get("summary") or ""
        if not _is_relevant_news(title, summary, r["url"], aliases):
            continue
        rows.append({
            "date": leaf(r.get("date"), r["url"]),
            "title": leaf(title, r["url"]),
            "summary": leaf(summary, r["url"]),
            "category": leaf(categorize_news(title, summary), r["url"]),
            "url": leaf(r["url"], r["url"]),
        })
    if rows:
        template["external_enrichment"]["latest_news"]["news"] = rows
        print(f"  latest_news: {len(rows)} articles via Google News RSS")
    else:
        # Confirmed bug: JPM run showed latest_news 0/4 with zero log
        # line — same silent-skip pattern as awards, indistinguishable
        # from a crash. Always report outcome.
        reason = "no articles matched query" if not results else f"{len(results)} articles found, all failed relevance filter"
        print(f"  latest_news: 0 articles ({reason})")


def _is_award_subject(title, aliases):
    """True only if any known alias is the SUBJECT of the headline, not
    just mentioned somewhere in it. Two known false-positive shapes, both
    fixed here:
    1. "Wipro Wins Microsoft Switzerland Partner of the Year" — another
       company is the subject before the wins/named verb.
    2. "LinkedIn's Top 10 US Companies... Highlight JPMorgan, Microsoft,
       and Amazon" — no wins/named verb, but company is just one name in
       a list, not the article's actual subject."""
    m = re.search(r"\b(wins?|named)\b", title, re.IGNORECASE)
    subject_text = title[:m.start()] if m else title
    subject_lower = subject_text.lower()
    hit = next((a for a in aliases if a.lower() in subject_lower), None)
    if not hit:
        return False
    # list-style mention: "..., Company, and ..." / "..., Company and ..."
    if re.search(rf",\s*{re.escape(hit)}\s*,?\s+and\s", title, re.IGNORECASE):
        return False
    return True


def fill_awards(template, company_name, ticker=None):
    q = clean_company_name(company_name)
    aliases = _aliases_for(ticker, q)
    # Confirmed bug: NewsAPI's `q` param is NOT literal-phrase matching —
    # "Microsoft wins award" as a bare string returned 0 articles (weak
    # relevance search, not what worked with RSS's looser interpretation).
    # NewsAPI DOES support real query syntax: quoted phrases + AND/OR/NOT
    # with parentheses. Use it properly instead of hoping loose text works.
    query = f'{_alias_query_clause(aliases)} AND (award OR wins OR winner OR recognized OR "named best" OR "top employer")'
    results = news_search(query, max_results=15)
    seen, deduped = set(), []
    for r in results:
        if r.get("url") and r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    rows = []
    for r in deduped:
        title = r.get("title", "")
        if not _is_award_subject(title, aliases):
            continue
        rows.append({
            "date": leaf(r.get("date"), r["url"]),
            "award": leaf(title, r["url"]),
            "brief": leaf(r.get("summary"), r["url"]),
        })
    if rows:
        template["external_enrichment"]["awards_and_accolades"]["awards"] = rows
        print(f"  awards_and_accolades: {len(rows)} items via Google News RSS")
    else:
        # Confirmed bug: JPM and WMT runs showed NO awards log line at
        # all — indistinguishable from a silent crash. Always report the
        # outcome so a zero-result run is visibly "0 found" not "did this
        # even run?". Distinguish empty-query-results from
        # all-filtered-out so the real cause is visible.
        reason = "no articles matched query" if not deduped else f"{len(deduped)} articles found, all failed subject/award filter"
        print(f"  awards_and_accolades: 0 items ({reason})")


# ---------------------------------------------------------------------------
# leadership details — Wikipedia REST summary API, written onto
# organization.people[] / organization.technology_team[] directly.
# ---------------------------------------------------------------------------
def _name_variants(name):
    """Try the full SEC legal name first, then progressively drop middle
    initials/names — Wikipedia article titles use common names ("Amy Hood"),
    SEC filings use full legal names ("Amy E. Hood"). Confirmed bug: querying
    only the SEC form 404s for anyone whose Wikipedia title omits a middle
    initial. e.g. "Amy E. Hood" -> also try "Amy Hood"."""
    parts = name.split()
    variants = [name]
    if len(parts) > 2:
        # drop every middle token that looks like an initial or short name
        variants.append(f"{parts[0]} {parts[-1]}")
    seen = set()
    out = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _wikipedia_summary(name):
    for variant in _name_variants(name):
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(variant.replace(' ', '_'))}"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "CompanyIntelPipeline/1.0"}, verify=False)
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("type") == "disambiguation":
                continue
            return {
                "extract": data.get("extract"),
                "photo_url": (data.get("thumbnail") or {}).get("source"),
                "page_url": (data.get("content_urls", {}).get("desktop") or {}).get("page"),
            }
        except Exception as e:
            print(f"[warn] wikipedia lookup failed for '{variant}': {e}")
    return None


def fill_vision_from_wikidata(template, wikidata_qid):
    """Best-effort vision fill via Wikidata's 'motto text' (P1451) property.
    Rarely populated for tech companies (confirmed in data source testing)
    but free and correct when present — stays null otherwise, no guessing.
    Requires the company's Wikidata Q-id; caller looks it up via wbsearchentities."""
    if not wikidata_qid:
        return
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{wikidata_qid}.json"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "CompanyIntelPipeline/1.0"}, verify=False)
        if r.status_code != 200:
            return
        entity = r.json()["entities"][wikidata_qid]
        claims = entity.get("claims", {}).get("P1451")
        if not claims:
            return
        motto = claims[0]["mainsnak"]["datavalue"]["value"]["text"]
        vision_leaf = template["from_annual_report"]["mission_vision_values"]["vision"]
        if not vision_leaf.get("value"):
            vision_leaf["value"] = motto
            vision_leaf["source"] = url
            vision_leaf["as_of"] = now_iso()
            print(f"  vision: filled from Wikidata motto ({wikidata_qid})")
    except Exception as e:
        print(f"[warn] wikidata motto lookup failed: {e}")


def _wikidata_qid_for(company_name):
    """Resolve a company name to its Wikidata Q-id via the public search API —
    no hardcoded ids, works for any company, not just Microsoft."""
    url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "wbsearchentities", "search": company_name, "language": "en",
        "type": "item", "format": "json", "limit": 1,
    }
    try:
        r = requests.get(url, params=params, timeout=10, headers={"User-Agent": "CompanyIntelPipeline/1.0"}, verify=False)
        results = r.json().get("search", [])
        return results[0]["id"] if results else None
    except Exception as e:
        print(f"[warn] wikidata search failed for '{company_name}': {e}")
        return None


def _linkedin_search_link(name, company_name):
    q = quote(f"{name} {company_name}")
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def fill_leadership_details(template, company_name):
    for list_key in ("people", "technology_team"):
        rows = template["from_annual_report"]["organization"].get(list_key, [])
        filled = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name", {}).get("value")
            if not name:
                continue
            wiki = _wikipedia_summary(name)
            if wiki and wiki.get("extract"):
                if not row.get("brief", {}).get("value"):
                    row["brief"] = leaf(wiki["extract"], wiki["page_url"])
                if wiki.get("photo_url") and "photo_url" in row:
                    row["photo_url"] = leaf(wiki["photo_url"], wiki["page_url"])
                filled += 1
            if "linkedin_url" in row and not row.get("linkedin_url", {}).get("value"):
                link = _linkedin_search_link(name, company_name)
                row["linkedin_url"] = leaf(link, link)
        if rows:
            print(f"  organization.{list_key}: {filled}/{len(rows)} matched a Wikipedia bio/photo")


# ---------------------------------------------------------------------------
# competitors — names from filing text (LLM), financials via SEC ticker
# lookup + yfinance if publicly traded.
# ---------------------------------------------------------------------------
def fill_competitors(template, raw_dir, source_url, as_of, company_name, general_knowledge_fallback=True):
    html_path = os.path.join(raw_dir, "filing.html")
    if not os.path.exists(html_path):
        print("[warn] filing.html not found, skipping competitors")
        return
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    items_text = split_items(html)
    chunk = (items_text.get("Item 1", "") + "\n" + items_text.get("Item 1A", ""))[:max_chars_for("Item 1")]
    if not chunk.strip():
        return

    prompt = f"""From this 10-K text, list ONLY companies EXPLICITLY named as a
competitor or as competition. Do not include customers, partners, or suppliers.
Do not infer from industry category — only names the text itself calls out.
Return ONLY a JSON array of company name strings. If none named, return [].

Text:
\"\"\"{chunk}\"\"\"

JSON array:"""
    raw = call_llm(prompt)
    try:
        names = json.loads(raw)
        names = [n for n in names if isinstance(n, str) and n.strip()]
    except json.JSONDecodeError:
        print("[warn] competitor extraction returned non-JSON, skipping")
        return

    if not names:
        template["external_enrichment"]["competitors"]["key_competitors"] = []
        print("  competitors: 0 named explicitly in the filing (correct null, not a fetch failure)")
        return

    rows = []
    for name in names:
        row = {
            "company_name": leaf(name, source_url, as_of),
            "revenue": leaf(None, None, None),
            "employees": leaf(None, None, None),
            "market_cap": leaf(None, None, None),
            "ict_budget": leaf(None, None, None),  # no free source, always null
        }
        try:
            candidates = search_company(name, limit=1)
            if candidates:
                peer_ticker = candidates[0]["ticker"]
                info, src_tag = _yfinance_info(peer_ticker)
                if info:
                    src = f"{src_tag}: {peer_ticker}"
                    row["revenue"] = leaf(info.get("totalRevenue"), src)
                    row["employees"] = leaf(info.get("fullTimeEmployees"), src)
                    row["market_cap"] = leaf(info.get("marketCap"), src)
        except SystemExit:
            pass  # search_company raises if no match — treat as "not public"
        except Exception as e:
            print(f"[warn] competitor ticker lookup failed for '{name}': {e}")
        rows.append(row)

    if not rows and general_knowledge_fallback:
        # Filing named zero competitors (common — many 10-Ks describe
        # competition generically without naming rivals). This is an
        # EXPLICIT opt-in fallback: asks the LLM's general knowledge, not
        # the filing text. Every row is tagged with a source that says so,
        # so it can never be mistaken for filing-verified data downstream —
        # matches the no-silent-guessing rule by being loud about the guess
        # instead of avoiding it.
        prompt = f"""Name up to 5 widely recognized direct competitors of {company_name}
(the company, not its products). Use general knowledge, not any specific
document. Return ONLY a JSON array of company name strings."""
        raw = call_llm(prompt)
        try:
            gk_names = [n for n in json.loads(raw) if isinstance(n, str) and n.strip()]
        except json.JSONDecodeError:
            print(f"[warn] competitor general-knowledge fallback returned non-JSON, skipping. Raw: {raw[:200]!r}")
            gk_names = []
        for name in gk_names:
            row = {
                "company_name": leaf(name, "LLM general knowledge — NOT filing-verified"),
                "revenue": leaf(None, None, None),
                "employees": leaf(None, None, None),
                "market_cap": leaf(None, None, None),
                "ict_budget": leaf(None, None, None),
            }
            try:
                candidates = search_company(name, limit=1)
                if candidates:
                    peer_ticker = candidates[0]["ticker"]
                    info, src_tag = _yfinance_info(peer_ticker)
                    if info:
                        src = f"{src_tag}: {peer_ticker}"
                        row["revenue"] = leaf(info.get("totalRevenue"), src)
                        row["employees"] = leaf(info.get("fullTimeEmployees"), src)
                        row["market_cap"] = leaf(info.get("marketCap"), src)
            except SystemExit:
                pass
            except Exception as e:
                print(f"[warn] competitor ticker lookup failed for '{name}': {e}")
            rows.append(row)
        if rows:
            print(f"  competitors: 0 named in filing — used general-knowledge fallback for {len(rows)} (marked non-filing-verified)")

    if rows:
        template["external_enrichment"]["competitors"]["key_competitors"] = rows
        print(f"  competitors: {len(rows)} total, financials filled where publicly traded")
    else:
        print("  competitors: 0 named explicitly in the filing (correct null, not a fetch failure)")


# ---------------------------------------------------------------------------
# deals — USAspending.gov, US federal contracts only
# ---------------------------------------------------------------------------
def fill_deals(template, company_name):
    q = clean_company_name(company_name)
    url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    body = {
        "filters": {
            "recipient_search_text": [q],
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2018-01-01", "end_date": datetime.now().strftime("%Y-%m-%d")}],
        },
        "fields": ["Award ID", "Recipient Name", "Start Date", "End Date", "Award Amount", "Description"],
        "limit": 10,
    }
    try:
        r = requests.post(url, json=body, timeout=20, verify=False)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        print(f"[warn] USAspending lookup failed: {e}")
        return
    rows = []
    for r in results:
        desc = (r.get("Description") or "").strip()
        amount = r.get("Award Amount")
        amount_str = f"${amount:,.0f}" if isinstance(amount, (int, float)) else f"${amount}"
        details = f"{desc} — contract value {amount_str}" if desc else f"Contract value {amount_str}"
        rows.append({
            "vendor": leaf(r.get("Recipient Name") or q, url),
            "start_date": leaf(r.get("Start Date"), url),
            "end_date": leaf(r.get("End Date"), url),
            "contract_details": leaf(details, url),
        })
    if rows:
        template["external_enrichment"]["deals"]["deals"] = rows
        print(f"  deals: {len(rows)} US federal contracts via USAspending.gov")
    else:
        print("  deals: no US federal contracts found (correct null if no govt business)")


# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python enrich.py TICKER YEAR")
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    path = os.path.join(EXTRACTED_DIR, ticker, year, "extracted.json")
    if not os.path.exists(path):
        raise SystemExit(f"Run extract_annual_report.py first — {path} missing")
    with open(path, encoding="utf-8") as f:
        template = json.load(f)

    raw_dir = os.path.join(RAW_DIR, ticker, year)
    with open(os.path.join(raw_dir, "metadata.json")) as f:
        meta = json.load(f)
    source_url = meta["source_url"]
    as_of = meta["filing_date"]
    company_name = meta.get("company", ticker)

    fill_investor_information(template, ticker)
    fill_competitors(template, raw_dir, source_url, as_of, company_name)
    fill_latest_news(template, company_name, ticker)
    fill_awards(template, company_name, ticker)
    fill_leadership_details(template, company_name)
    fill_deals(template, company_name)
    qid = _wikidata_qid_for(company_name)
    fill_vision_from_wikidata(template, qid)
    # esg_ratings, industry_market_data: intentionally untouched — no free
    # source confirmed, stay null (manual/paid-source only).

    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"\nEnriched: {path}")


if __name__ == "__main__":
    main()
