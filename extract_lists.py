"""
extract_lists.py — Stage 3b: fills the list/array fields extract_annual_report.py
flags as [todo] (segments[], people[], board_of_directors[], acquisitions[],
partnerships[], technology_initiatives[], challenges[]).

Same rule as extract_annual_report.py: only the relevant Item chunk goes to
the LLM, never the full filing.

Usage:
    python extract_lists.py AAPL 2025
(run AFTER extract_annual_report.py — loads its output, adds list fields, resaves)
"""

import json
import os
import sys

from extract_annual_report import (
    call_llm, split_items, load_xbrl_facts, _lookup,
    RAW_DIR, EXTRACTED_DIR, max_chars_for,
)

# Item 1 narrative names historically significant acquisitions (e.g. old
# Mellanox deal) but often does NOT mention the current fiscal year's
# acquisitions by name — those only show up as line items on the cash flow
# statement. Cross-checking XBRL catches deals the narrative misses.
#
# Confirmed (this session, MSFT): "acquisitions" is NOT one standardized
# concept name across filers, same lesson as capex/D&A last session.
# NVDA/GOOGL use one of the 3 net-of-cash tags below; MSFT tags the SAME
# cash-flow line as AcquisitionsNetOfCashAcquiredAndPurchasesOfIntangibleAndOtherAssets
# instead — confirmed present with real FY values in MSFT's raw xbrl_facts.json
# (verified directly, not guessed). Added as a 4th tag, not a swap, since the
# original 3 are still correct for NVDA/GOOGL.
ACQUISITION_XBRL_TAGS = [
    "PaymentsToAcquireBusinessesNetOfCashAcquired",
    "PaymentsToAcquireBusinessTwoNetOfCashAcquired",
    "PaymentsToAcquireBusinessesGross",
    "AcquisitionsNetOfCashAcquiredAndPurchasesOfIntangibleAndOtherAssets",
]

# Named-deal consideration tag — a SEPARATE concept from the cash-flow tags
# above. Confirmed on MSFT: BusinessCombinationConsiderationTransferred1,
# dimensioned, dimension_member_label = "Activision Blizzard", period_end =
# the deal CLOSE DATE (2023-10-13), not a fiscal-year-end duration. This is
# real, reliable, and names the counterparty directly — but it will never
# pass a "period_end == latest FY end" filter since deal-close dates don't
# land on fiscal year-end. Collected separately, no period-matching required
# beyond "is this fiscal year's deal" (handled by simple recency check in
# xbrl_named_deal_facts below).
NAMED_DEAL_TAGS = [
    "BusinessCombinationConsiderationTransferred1",
    "BusinessCombinationConsiderationTransferred",
]

# field_path -> (item, object shape, description)
LIST_FIELDS = {
    "from_annual_report.business_segments.segments": (
        "Item 1",
        ["segment_name", "revenue", "growth_rate", "operating_income",
         "operating_margin", "description", "key_services"],
    ),
    # extracted with type/appointment_date still (needed for the officer
    # filter below) then STRIPPED down to the template's 5 fields before
    # wrapping — see filter_officers().
    "from_annual_report.organization.people": (
        "Signatures",
        ["name", "designation", "type", "responsibility", "brief", "appointment_date"],
    ),
    # Optional slide ("Organization Structure – Technology Team, if
    # available"). No dedicated filing section names a tech team — best
    # effort from Item 1 tech/leadership mentions. Often empty; that's a
    # genuine null, not a bug.
    "from_annual_report.organization.technology_team": (
        "Item 1",
        ["name", "designation", "brief"],
    ),
    "from_annual_report.technology.technologies_in_use": (
        "Item 1",
        ["technology", "category", "brief"],
    ),
    "from_annual_report.technology.technology_initiatives": (
        "Item 1",
        ["date", "title", "details"],
    ),
    "from_annual_report.partnerships.partnerships": (
        "Item 1",
        ["partner", "date", "partnership_type", "brief"],
    ),
    "from_annual_report.acquisitions.acquisitions": (
        "Item 1",
        ["year", "company_name", "acquisition_value", "brief"],
    ),
    "from_annual_report.business_challenges.challenges": (
        "Item 1A",
        ["challenge", "impact", "brief"],
    ),
}

# Officer titles kept on the Leadership Team slide — everyone else pulled
# from the Signatures block (other directors, non-principal signers) is
# dropped. Matched case-insensitively, substring match against designation.
OFFICER_TITLES = [
    "chief executive officer",
    "chief financial officer",
    "chief accounting officer",
]


def filter_officers(rows):
    """Keep only CEO / CFO (Principal Financial Officer) / Chief Accounting
    Officer (Principal Accounting Officer) rows. Drops the rest of the
    Signatures block (other board members, non-principal signers)."""
    kept = []
    for row in rows:
        designation = (row.get("designation") or "").lower()
        if any(title in designation for title in OFFICER_TITLES):
            kept.append({
                "name": row.get("name"),
                "designation": row.get("designation"),
                "brief": row.get("brief"),
                "linkedin_url": None,   # filled later by enrich.py
                "photo_url": None,      # filled later by enrich.py
            })
    return kept


SEGMENT_REVENUE_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"
SEGMENT_OPINCOME_TAG = "OperatingIncomeLoss"


def xbrl_segment_facts(raw_dir):
    """Real per-segment revenue/operating income from XBRL's business-segment
    axis (us-gaap:StatementBusinessSegmentsAxis) — structured filer-reported
    data, not LLM-parsed from Item 1 narrative prose.

    Confirmed bug (MSFT, this session): the old Item-1-only LLM pass returned
    "XBOX" / "Search Advertising" — real product lines mentioned in the
    narrative, but NOT the filer's actual reportable segments (Productivity
    and Business Processes / Intelligent Cloud / More Personal Computing).
    That's wrong data, not just incomplete data — Item 1 prose describes
    products, Item 8's segment footnote (tagged via this axis) is the actual
    reporting structure with real revenue/op-income per segment. Two most
    recent fiscal years collected so growth_rate is computable directly."""
    path = os.path.join(raw_dir, "xbrl_facts.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        records = json.load(f)

    axis_key = "dim_us-gaap_StatementBusinessSegmentsAxis"
    fy_ends = sorted({
        r.get("period_end") for r in records
        if r.get(axis_key) and r.get("fiscal_period") == "FY"
        and r.get("period_type") == "duration" and r.get("period_end")
    }, reverse=True)
    if not fy_ends:
        return {}
    latest, prior = fy_ends[0], (fy_ends[1] if len(fy_ends) > 1 else None)

    segments = {}
    for r in records:
        label_raw = r.get(axis_key)
        if not label_raw or r.get("fiscal_period") != "FY" or r.get("period_type") != "duration":
            continue
        label = r.get("dimension_member_label") or label_raw  # human name, e.g.
        # "Productivity and Business Processes" — falls back to the raw
        # member code only if a filer omits the label, so it's never lost
        concept = (r.get("concept") or "").split(":")[-1]
        val = r.get("numeric_value")
        if val is None:
            continue
        end = r.get("period_end")
        seg = segments.setdefault(label, {})
        if concept == SEGMENT_REVENUE_TAG:
            if end == latest:
                seg["revenue"] = val
            elif end == prior:
                seg["revenue_prior"] = val
        elif concept == SEGMENT_OPINCOME_TAG and end == latest:
            seg["operating_income"] = val
    return segments


def build_segment_rows(xbrl_segs, llm_rows, object_fields):
    """XBRL is authoritative for name/revenue/operating_income/growth_rate/
    operating_margin. LLM Item-1 rows are only used to borrow qualitative
    fields (description, key_services) for a matching segment name — never
    to supply the segment name or numbers themselves.

    Confirmed bug: GOOGL's segment axis returns 9 raw dimension members —
    some are real reportable segments (Google Services, Google Cloud,
    Other Bets), others are sub-line-items rolled into those totals
    (Google Search & other, YouTube ads, Google Network, Google
    advertising, Google subscriptions/platforms/devices) or generic
    rollup labels (Operating Segments). Treating every member as a segment
    row double/triple-counted revenue (Google Services + its own
    sub-components all listed as if independent). Filter: a member is a
    real reportable segment only if it has an operating_income value
    (rollups/subtotals in this filer's XBRL don't carry one) OR its
    revenue doesn't nest inside another member's revenue (checked by
    keeping only members whose revenue doesn't sum-match a larger
    sibling — simplified here to: keep members with operating_income
    present, plus any member with no operating_income only if NO other
    member's revenue is within 1% of it, since subtotal labels almost
    always duplicate a real segment's total)."""
    # Denylist runs FIRST, unconditionally, before either branch below —
    # confirmed bug: this check previously lived only inside the
    # no-op-income loop, so a generic rollup label that DOES carry an
    # operating_income value (GOOGL's "Operating Segments", op_income
    # -7.5B) sailed straight into has_op_income untouched and was never
    # rejected. Drop denylisted labels outright regardless of which XBRL
    # fields they carry.
    # Snapshot BEFORE denylist runs — needed below to detect the
    # single-real-XBRL-segment case (Apple: only member is generically
    # labeled "Operating segments") vs the many-members-plus-rollup case
    # (JPM/GOOGL: a real denylisted label sits ALONGSIDE real segments).
    # Confirmed bug: denylist unconditionally dropped Apple's one-and-only
    # member, leaving 0 items, so single_segment_bypass (len==1) never
    # fired and Apple's segment vanished entirely instead of falling back.
    pre_denylist_count = len(xbrl_segs)

    GENERIC_ROLLUP_LABELS = (
        "operating segments", "segments", "total", "consolidated",
        "corporate", "corporate and other", "all other", "unallocated",
        "eliminations", "reconciling items",
    )
    denylisted = {
        k: v for k, v in xbrl_segs.items()
        if k.strip().lower() in GENERIC_ROLLUP_LABELS
    }
    xbrl_segs = {
        k: v for k, v in xbrl_segs.items()
        if k.strip().lower() not in GENERIC_ROLLUP_LABELS
    }

    # If denylisting wiped out the ONLY member that existed (Apple case:
    # pre_denylist_count was 1), restore it — a lone generically-labeled
    # segment is still the real segment for a single-segment filer, not a
    # rollup to discard. Only skip restoring if there were other members
    # (a genuine rollup sitting alongside real ones, e.g. JPM/GOOGL).
    if not xbrl_segs and pre_denylist_count == 1 and denylisted:
        xbrl_segs = dict(denylisted)

    # Confirmed bug (WMT=1 not 3): the ratio branch (rev/orev < 0.9) treated
    # ANY member smaller than a sibling as a "subset" of it — but real,
    # independent segments are routinely different sizes (Sam's Club U.S.
    # at 13% of revenue is not a sub-line of Walmart U.S. at 68%, it's its
    # own segment). Size difference alone is not containment. Only an
    # exact-or-near-exact revenue MATCH is real evidence that one member
    # is a duplicate/rollup of another (e.g. "Google advertising" summing
    # to the same total as "Google Search + YouTube ads + Google Network"
    # combined, or a sub-line whose revenue equals its parent's).
    def _is_subset_of_larger(k, v, pool):
        rev = v.get("revenue")
        if rev is None:
            return False
        for ok, other in pool.items():
            if ok == k:
                continue
            orev = other.get("revenue")
            if orev is None or orev <= rev:
                continue
            if abs(orev - rev) / max(orev, 1) < 0.01:
                return True
        return False

    filtered_segs = {
        k: v for k, v in xbrl_segs.items()
        if not _is_subset_of_larger(k, v, xbrl_segs)
    }
    # Single-XBRL-segment filers (Apple): don't require name match — take
    # the first LLM row's description/key_services regardless of name
    # similarity. Confirmed bug: Apple's XBRL label is generic "Operating
    # segments", the LLM's real Item-1 product-based segment_name (e.g.
    # "iPhone") never substring-matches it, so description/key_services
    # were silently dropped even though the LLM extracted them fine.
    single_segment_bypass = len(filtered_segs) == 1

    used = set()
    rows = []
    for seg_name, data in filtered_segs.items():
        match = None
        if single_segment_bypass and llm_rows:
            match = next((r for r in llm_rows if isinstance(r, dict)), None)
        else:
            for i, item in enumerate(llm_rows):
                if i in used or not isinstance(item, dict):
                    continue
                llm_name = (item.get("segment_name") or "").lower()
                if llm_name and (llm_name in seg_name.lower() or seg_name.lower() in llm_name):
                    match = item
                    used.add(i)
                    break
        row = {f: None for f in object_fields}
        row["segment_name"] = seg_name
        row["revenue"] = data.get("revenue")
        row["operating_income"] = data.get("operating_income")
        rev_prev = data.get("revenue_prior")
        if row["revenue"] is not None and rev_prev:
            row["growth_rate"] = round((row["revenue"] - rev_prev) / rev_prev * 100, 2)
        if row["revenue"] and row["operating_income"] is not None:
            row["operating_margin"] = round(row["operating_income"] / row["revenue"], 4)
        if match:
            row["description"] = match.get("description")
            row["key_services"] = match.get("key_services")
        rows.append(row)
    return rows


def get_container(template, dotted):
    """Navigate to parent dict and final key for a list field."""
    parts = dotted.split(".")
    node = template
    for key in parts[:-1]:
        node = node[key]
    return node, parts[-1]


def extract_list_section(item_name, chunk_text, object_fields, list_name):
    chunk_text = chunk_text[:max_chars_for(item_name)]
    fields_str = ", ".join(object_fields)
    prompt = f"""Extract a list of "{list_name}" from this 10-K section ({item_name}).
Return ONLY a JSON array. Each array element is an object with these keys: {fields_str}.
Use null for any key you cannot find. If nothing relevant found, return [].
Do not invent data.

Section text:
\"\"\"{chunk_text}\"\"\"

JSON array:"""
    raw = call_llm(prompt)
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        print(f"[warn] LLM returned non-JSON list for {list_name}, skipping")
        return []


def _business_combinations_snippet(item8_text, window=7000):
    """Item 1 rarely names smaller acquisitions — counterparty names for
    those usually only appear in the Item 8 'Business Combinations' /
    'Acquisitions' note to the financial statements, which Item 1 never
    covers. Blind-truncating Item 8 from the start would cut off before
    reaching that note (Item 8 is the full financial statements + all
    notes, often 100+ pages). Find the note by keyword and window around
    it instead of truncating from position 0."""
    if not item8_text:
        return ""
    lower = item8_text.lower()
    for keyword in ("business combination", "acquisitions and dispositions", "note to acquisitions"):
        idx = lower.find(keyword)
        if idx != -1:
            start = max(0, idx - 500)
            return item8_text[start:start + window]
    return ""


def _money_to_float(val):
    """Item 8 narrative extraction sometimes returns '$ 75.4 billion'
    instead of a bare number — confirmed bug: comparing that string
    directly against XBRL's numeric 75400000000 always raised ValueError,
    silently failed the dedup match, and created a duplicate row instead
    of filling the existing one. Normalize both sides before comparing."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower().replace("$", "").replace(",", "")
    multiplier = 1
    for word, mult in (("billion", 1e9), ("million", 1e6), ("thousand", 1e3)):
        if word in s:
            s = s.replace(word, "").strip()
            multiplier = mult
            break
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def xbrl_named_deal_facts(raw_dir):
    """Named-deal consideration facts (e.g. MSFT/Activision) — dimensioned,
    carries the counterparty name via dimension_member_label, dated by the
    deal's own close date rather than a fiscal-year-end duration. No FY
    period-matching applies here; every fact found under these tags is a
    real, named transaction — collected as-is."""
    path = os.path.join(raw_dir, "xbrl_facts.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    rows = []
    for r in records:
        concept = (r.get("concept") or "").split(":")[-1]
        if concept not in NAMED_DEAL_TAGS:
            continue
        val = r.get("numeric_value")
        if val is None:
            val = r.get("value")
        if not val:
            continue
        end = r.get("period_end") or r.get("period_instant")
        rows.append({
            "tag": concept,
            "value": val,
            "label": r.get("dimension_member_label"),
            "date": end,
            "year": end[:4] if end else None,
        })
    return rows


def xbrl_dimensioned_acquisition_facts(raw_dir):
    """Full-fiscal-year dimensioned acquisition facts only.

    Previous version skipped ALL is_dimensioned facts, which is why Alphabet's
    real $32B PaymentsToAcquireBusinessesGross tag never surfaced. Root cause
    wasn't "dimensioned = skip" — it's "PARTIAL-PERIOD dimensioned = skip".
    Same period-matching rule load_xbrl_facts() already uses for revenue/income
    (fiscal_period == 'FY', period_end == latest FY end) applied here to
    dimensioned rows too. A Q1-only deposit fact fails this filter and is
    correctly dropped; a full-year dimensioned fact passes and is reliable.

    Bonus: dimensioned facts carry dimension_member_label (e.g. the acquired
    company name on the acquisition axis) — use it for company_name instead
    of an "UNVERIFIED" placeholder, when present."""
    path = os.path.join(raw_dir, "xbrl_facts.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        records = json.load(f)

    fy_ends = [
        r.get("period_end") for r in records
        if r.get("period_type") == "duration" and r.get("fiscal_period") == "FY"
        and not r.get("is_dimensioned", False) and r.get("period_end")
    ]
    if not fy_ends:
        return []
    latest_fy_end = max(fy_ends)

    rows = []
    for r in records:
        if not r.get("is_dimensioned", False):
            continue
        concept = (r.get("concept") or "").split(":")[-1]
        if concept not in ACQUISITION_XBRL_TAGS:
            continue
        # full fiscal year only — excludes partial-period facts like a
        # Q1-only acquisition deposit
        if r.get("period_type") != "duration" or r.get("fiscal_period") != "FY":
            continue
        if r.get("period_end") != latest_fy_end:
            continue
        val = r.get("numeric_value")
        if val is None:
            val = r.get("value")
        if val:
            rows.append({
                "tag": concept,
                "value": val,
                "label": r.get("dimension_member_label"),
                "year": latest_fy_end[:4],
            })
    return rows


def xbrl_acquisition_rows(raw_dir, source_url, as_of, object_fields):
    """Build acquisition rows straight from cash-flow XBRL facts — catches
    current-year deals the Item 1 narrative doesn't name. acquisition_value
    (and year) come from XBRL; company_name comes from dimension_member_label
    when the fact is dimensioned and carries one, otherwise left null for
    manual reconciliation against the narrative LLM pass — never a guess."""
    current_facts, prior_facts, second_prior_facts, second_prior_end, _geo_revenue = load_xbrl_facts(
        os.path.join(raw_dir, "xbrl_facts.json")
    )
    rows = []
    for period_facts, year_label in (
        (current_facts, None),
        (prior_facts, None),
        (second_prior_facts, second_prior_end[:4] if second_prior_end else None),
    ):
        if not period_facts:
            continue
        for tag in ACQUISITION_XBRL_TAGS:
            val = period_facts.get(tag)  # exact tag only, no fallback chain — these are period-specific line items
            if val and val != 0:
                row = {f: None for f in object_fields}
                row["acquisition_value"] = val
                if "year" in row:
                    row["year"] = year_label
                row["_xbrl_tag"] = tag  # not a template field, dropped before wrap
                rows.append(row)

    # full-year dimensioned facts (e.g. Alphabet's Wiz-class deals) — the
    # gap deliberately left open last session, now filled reliably
    for d in xbrl_dimensioned_acquisition_facts(raw_dir):
        row = {f: None for f in object_fields}
        row["acquisition_value"] = d["value"]
        if "year" in row:
            row["year"] = d["year"]
        if d["label"] and "company_name" in row:
            row["company_name"] = d["label"]
        row["_xbrl_tag"] = d["tag"]
        rows.append(row)

    # named-deal consideration facts (e.g. MSFT/Activision) — dated by deal
    # close, always carries a counterparty name when present
    for d in xbrl_named_deal_facts(raw_dir):
        row = {f: None for f in object_fields}
        row["acquisition_value"] = d["value"]
        if "year" in row:
            row["year"] = d["year"]
        if "acquisition_date" in row:
            row["acquisition_date"] = d["date"]
        if d["label"] and "company_name" in row:
            row["company_name"] = d["label"]
        row["_xbrl_tag"] = d["tag"]
        rows.append(row)

    return rows


def wrap_items(raw_items, object_fields, source_url, as_of):
    """Wrap each plain dict into value/source/as_of leaf format matching template."""
    wrapped = []
    for item in raw_items:
        obj = {}
        for field in object_fields:
            val = item.get(field) if isinstance(item, dict) else None
            obj[field] = {"value": val, "source": source_url, "as_of": as_of}
        wrapped.append(obj)
    return wrapped


def main():
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python extract_lists.py TICKER YEAR")
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    extracted_path = os.path.join(EXTRACTED_DIR, ticker, year, "extracted.json")
    if not os.path.exists(extracted_path):
        raise SystemExit(f"Run extract_annual_report.py first — {extracted_path} missing")
    with open(extracted_path, encoding="utf-8") as f:
        template = json.load(f)

    raw_dir = os.path.join(RAW_DIR, ticker, year)
    with open(os.path.join(raw_dir, "metadata.json")) as f:
        meta = json.load(f)
    with open(os.path.join(raw_dir, "filing.html"), encoding="utf-8") as f:
        html = f.read()

    source_url = meta["source_url"]
    as_of = meta["filing_date"]
    items_text = split_items(html)

    for field_path, (item_name, object_fields) in LIST_FIELDS.items():
        chunk = items_text.get(item_name)
        list_name = field_path.split(".")[-1]
        if not chunk:
            print(f"[warn] {item_name} not found, skipping {list_name}")
            continue
        raw_items = extract_list_section(item_name, chunk, object_fields, list_name)

        if field_path == "from_annual_report.business_segments.segments":
            xbrl_segs = xbrl_segment_facts(raw_dir)
            if xbrl_segs:
                raw_items = build_segment_rows(xbrl_segs, raw_items, object_fields)
                print(f"  [xbrl] segments replaced with real reportable segments: {list(xbrl_segs.keys())}")
            else:
                print("  [warn] no dimensioned segment XBRL found — keeping Item 1 narrative pass (verify segment names manually)")

        if field_path == "from_annual_report.organization.people":
            raw_items = filter_officers(raw_items)
            print(f"  [filter] people: kept {len(raw_items)} officer row(s) (CEO/CFO/CAO only)")

        if field_path == "from_annual_report.acquisitions.acquisitions":
            item8_chunk = _business_combinations_snippet(items_text.get("Item 8", ""))
            if item8_chunk:
                item8_rows = extract_list_section("Item 8 (Business Combinations note)", item8_chunk, object_fields, "acquisitions")
                # match by dollar value against rows the Item 1 pass already
                # has but left company_name empty — fills the name without
                # creating a duplicate row. Uses _money_to_float() since
                # Item 8's narrative extraction sometimes returns
                # "$75.4 billion" instead of a bare number — comparing that
                # against XBRL's raw numeric always failed silently before.
                for i8_row in item8_rows:
                    if not isinstance(i8_row, dict) or not i8_row.get("company_name"):
                        continue
                    i8_val = _money_to_float(i8_row.get("acquisition_value"))
                    matched = False
                    if i8_val is not None:
                        for row in raw_items:
                            if not isinstance(row, dict) or row.get("acquisition_value") in (None, ""):
                                continue
                            row_val = _money_to_float(row["acquisition_value"])
                            if row_val is None:
                                continue
                            if abs(row_val - i8_val) < max(i8_val * 0.05, 1):
                                if not row.get("company_name"):
                                    row["company_name"] = i8_row["company_name"]
                                matched = True  # same deal either way — never append a duplicate
                                break
                    if not matched:
                        raw_items.append(i8_row)
                print(f"  [item8] Business Combinations note: {sum(1 for r in item8_rows if isinstance(r, dict) and r.get('company_name'))} named counterpart(y/ies) found")

            xbrl_rows = xbrl_acquisition_rows(raw_dir, source_url, as_of, object_fields)
            # match XBRL current-year row (year_label None => most recent fiscal
            # year, i.e. the deal a fresh 10-K would report) against any LLM row
            # that already has a value close to it, to avoid a duplicate entry
            # for the same deal. Simple heuristic: same order of magnitude value
            # within 5% => treat as already covered by the narrative pass.
            llm_values = {
                round(v, -6)
                for r in raw_items
                if isinstance(r, dict)
                for v in [_money_to_float(r.get("acquisition_value"))]
                if v is not None
            }
            for row in xbrl_rows:
                xbrl_val = row.get("acquisition_value")
                if xbrl_val is None:
                    continue
                rounded = round(float(xbrl_val), -6)
                if rounded in llm_values:
                    continue  # narrative pass already has this deal's dollar figure

                # Confirmed bug: named-but-valueless rows (VIZIO, Flipkart,
                # PhonePe — LLM found the company name in prose but no
                # dollar figure nearby) were never checked against XBRL
                # rows at all, only unnamed rows were compared via
                # llm_values above. Try filling an EXISTING named row's
                # missing value first, before falling back to appending a
                # new unnamed row. Only fill if exactly one named row is
                # missing a value this round — with 2+ ambiguous
                # candidates, guessing which name goes with which number
                # is worse than leaving both null.
                named_missing_value = [
                    r for r in raw_items
                    if isinstance(r, dict) and r.get("company_name")
                    and r.get("acquisition_value") in (None, "")
                ]
                if len(named_missing_value) == 1:
                    named_missing_value[0]["acquisition_value"] = xbrl_val
                    llm_values.add(rounded)
                    continue

                row.pop("_xbrl_tag", None)
                # Confirmed bug: previous version wrote an explanatory
                # sentence into company_name's VALUE — that string would
                # literally render as the "company name" on the Key
                # Acquisitions slide. Leave value null (honest — the
                # counterparty genuinely isn't stated) and put the
                # explanation in source instead, where downstream renderers
                # don't display it as data.
                row["_unverified_name"] = not row.get("company_name")
                raw_items.append(row)
                print(f"  [xbrl] added acquisition row not caught by narrative pass: value={xbrl_val}")

            # Normalize acquisition_value to a bare number for every row —
            # Item 8 extraction sometimes returns "$75.4 billion" instead of
            # a number, which would render badly on the slide (and doesn't
            # match derive.py's numeric expectations elsewhere).
            for row in raw_items:
                if isinstance(row, dict) and row.get("acquisition_value") is not None:
                    normalized = _money_to_float(row["acquisition_value"])
                    if normalized is not None:
                        row["acquisition_value"] = normalized

            # Final dedupe safety net by company_name (case-insensitive) —
            # keep whichever duplicate has the most non-null fields.
            by_name, unnamed = {}, []
            for row in raw_items:
                if not isinstance(row, dict):
                    continue
                name = (row.get("company_name") or "").strip().lower()
                if not name:
                    unnamed.append(row)
                    continue
                existing = by_name.get(name)
                if not existing or sum(v is not None for v in row.values()) > sum(v is not None for v in existing.values()):
                    by_name[name] = row
            raw_items = list(by_name.values()) + unnamed

        wrap_fields = object_fields
        if field_path == "from_annual_report.organization.people":
            # filter_officers() reshaped rows to the template's 5 people
            # fields — wrap using THAT shape, not the 6-field raw extraction
            # shape (type/responsibility/appointment_date were extraction-
            # only, used for filtering, then dropped).
            wrap_fields = ["name", "designation", "brief", "linkedin_url", "photo_url"]
        wrapped = wrap_items(raw_items, wrap_fields, source_url, as_of)
        if field_path == "from_annual_report.acquisitions.acquisitions":
            for raw, wrapped_row in zip(raw_items, wrapped):
                if isinstance(raw, dict) and raw.get("_unverified_name"):
                    wrapped_row["company_name"]["source"] = (
                        f"{source_url} — XBRL acquisition_value present, "
                        f"counterparty name not stated in narrative text"
                    )
        parent, key = get_container(template, field_path)
        parent[key] = wrapped
        print(f"  {list_name}: {len(wrapped)} items")

    with open(extracted_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"\nUpdated: {extracted_path}")


if __name__ == "__main__":
    main()
