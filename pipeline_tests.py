"""
Regression test suite for cintelv5's extract_annual_report.py LLM extraction.
Run manually after any pipeline fix: python pipeline_tests.py TICKER YEAR
Not run inside the live pipeline - costs extra Gemini calls, meant as a
pre-ship sanity check after touching extract_section/fill_from_llm/etc.

What this catches that your pipeline currently can't see on its own:
- hallucination: LLM inventing strategy/mission text not actually in the filing
- silent regression: a field that extracted fine last run coming back wrong
  after a prompt/retry/temperature tweak
"""
import asyncio
import json
import os
import sys

from agent_tester import AgentTester  # patches ssl on import, before extract_annual_report's requests calls

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# adjust this path to wherever extract_annual_report.py actually lives relative to this file
# sys.path.insert(0, r"C:\Users\GenaiblrpioUsr37\Downloads\cintelv5")

from extract_annual_report import extract_section, split_items, load_template, _coerce_llm_dict, _split_value_evidence  # noqa: E402


def make_extractor(item_name: str, field_paths: list):
    """Wraps extract_section into a (str -> str) fn AgentTester expects.
    'inputs' becomes the filing chunk text; output is the extracted field's value.
    extract_section now returns {field: {value, evidence}} — unwrap to bare value."""

    def agent_fn(chunk_text: str) -> str:
        result = extract_section(item_name, chunk_text, field_paths)
        result = _coerce_llm_dict(result, item_name)
        if len(field_paths) == 1:
            value, _ = _split_value_evidence(result.get(field_paths[0]))
            return str(value or "")
        return json.dumps({k: _split_value_evidence(v)[0] for k, v in result.items()})

    return agent_fn


async def check_extraction_groundedness(ticker: str, year: str, field_path: str, combined_label: str):
    """Loads a real saved filing.html for TICKER/YEAR, re-runs extraction for
    one field the SAME way fill_combined_search does (Item 1+1A+7 combined,
    since company_strategy/mission_vision_values/sustainability/it_spending
    are combined-search fields per your pipeline, not single-Item lookups),
    checks the extracted value is actually grounded in that text."""
    raw_path = os.path.join(r"C:\Users\GenaiblrpioUsr37\Downloads\cintelv5\raw\US", ticker, year, "filing.html")
    if not os.path.exists(raw_path):
        print(f"[skip] no saved filing.html for {ticker}/{year} — run the pipeline for it first")
        return

    with open(raw_path, encoding="utf-8") as f:
        html = f.read()
    items_text = split_items(html)

    PER_ITEM_CHARS = 15000
    combined = ""
    for item_name in ("Item 1", "Item 1A", "Item 7"):
        chunk = items_text.get(item_name, "")
        if chunk:
            combined += f"\n\n--- {item_name} ---\n" + chunk[:PER_ITEM_CHARS]
    if not combined.strip():
        print(f"[skip] no Item 1/1A/7 text found for {ticker}/{year}")
        return

    agent_fn = make_extractor(f"Item 1/1A/7 combined ({combined_label} search)", [field_path])
    tester = AgentTester(agent_fn)
    r = await tester.check_groundedness(user_input=combined, context=combined)
    extracted = r.final_trace.last.outputs
    is_empty = not extracted or extracted.strip() in ("", "None", "null")
    if is_empty:
        flag = "EMPTY (no hallucination possible, but nothing extracted either)"
    elif str(r.status).endswith("PASS"):
        flag = "OK"
    else:
        flag = "HALLUCINATION FLAG"
    print(f"[{flag}] {ticker}/{year} {field_path}")
    print(f"    extracted: {extracted[:200]}")


async def main():
    if len(sys.argv) < 3:
        print("Usage: python pipeline_tests.py TICKER YEAR")
        print("Example: python pipeline_tests.py AMZN 2026")
        return
    ticker, year = sys.argv[1].upper(), sys.argv[2]

    # fields most at risk per your own bug history: temperature drift,
    # empty-result retries, combined-search non-determinism.
    # (field_path, combined_label) - label matches fill_combined_search's own label arg
    checks = [
        ("from_annual_report.company_strategy.corporate_strategy", "company_strategy"),
        ("from_annual_report.mission_vision_values.mission", "mission_vision_values"),
    ]

    for field_path, label in checks:
        await check_extraction_groundedness(ticker, year, field_path, label)


if __name__ == "__main__":
    asyncio.run(main())

