"""
evaluate.py — Stage 12 (advisory): batched faithfulness/groundedness pass.

Runs LAST in run_pipeline.py, after the PPT is already built — nothing
downstream reads eval_score, and extract_slide_data.py only ever reads
template.json's `value` (never evidence/eval_score), so running evaluate
after the PPT stages doesn't lose anything.

Checks every leaf that carries BOTH value + evidence — lives only under
from_annual_report.*, from_quarterly_report.*, external_enrichment.*.
derived.* and analysis.* have NO evidence key at all (by schema design,
see template_v2.json) and are skipped automatically.

Advisory only, never blocks (run via run_step_optional in run_pipeline.py):
writes a 5th leaf key, eval_score (0-1 faithfulness), never touches value.
A low/missing score means "a human should double-check this", not
"pipeline failed".

Naming note: this checks FAITHFULNESS — is the value entailed by the
evidence snippet the extractor actually saw — not real-world correctness.
Catches hallucination, not fact-checking.

BATCHED, not per-leaf: previous version ran DeepEval's GEval metric once
per leaf (145 leaves -> 145 sequential Gemini calls -> ~16 min against the
4.5s/call rate limit). DeepEval's own async batching was tried and
rejected — concurrent tasks just queued up behind call_llm's single
global rate-limiter lock and triggered 503s instead of going faster.
That's a concurrency problem, not a payload problem: gemini-3.1-flash-
lite's ~1M token context has room to spare, so this version packs many
leaves into ONE prompt instead, and asks for one JSON array of scores
back. Cuts 145 calls down to ~187/BATCH_SIZE calls (default
BATCH_SIZE=25 -> ~8 calls), each still going through the same
rate-limited call_llm, so the throttle is respected, just paid far
fewer times. No DeepEval dependency needed anymore — this is a direct
call_llm JSON-mode pass, scored 0-1 same as before, same THRESHOLD
semantics.

Usage:
    python evaluate.py AAPL 2025
    python evaluate.py AAPL 2025 --batch-size 40
"""
import argparse
import json
import os
import sys

from extract_annual_report import call_llm, GeminiQuotaExhausted

FINAL_DIR = "final/US"
EVAL_SECTIONS = ("from_annual_report", "from_quarterly_report", "external_enrichment")
THRESHOLD = 0.7
DEFAULT_BATCH_SIZE = 12  # leaves per Gemini call. Was 25 — confirmed cause of
                          # silent misscoring (SentinelOne run: segment_name and
                          # awards[0].award both scored 0.00 despite evidence
                          # being an exact/near-exact match for the value, which
                          # is impossible for the stated criteria to produce
                          # correctly). Root cause: at 25 items, the model
                          # drops/reorders entries in its own numbered response
                          # under a long repetitive prompt — the id-based
                          # matching in score_batch() is correct, but if the
                          # model's returned id doesn't line up with the item
                          # it actually judged, the score silently lands on the
                          # wrong leaf. Smaller batches reduce how far entries
                          # can drift, and mismatch_count below now detects and
                          # reports when it still happens instead of staying silent.


def walk_all_scorable(node, path=""):
    """Yield every leaf with value+evidence (scored or not) — used for the
    final tally so 'X scored, Y flagged' counts the whole universe of
    checkable leaves, not just this run's batch."""
    if isinstance(node, dict):
        if "value" in node and "evidence" in node:
            if node.get("value") is not None and node.get("evidence"):
                yield node, path
            return
        for k, v in node.items():
            yield from walk_all_scorable(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk_all_scorable(item, f"{path}[{i}]")


def leaves_with_evidence(node, path=""):
    """Same walk, but skips leaves that already have a score — a run
    interrupted by quota exhaustion picks up where it left off next time
    instead of re-billing already-scored leaves."""
    if isinstance(node, dict):
        if "value" in node and "evidence" in node:
            if (node.get("value") is not None and node.get("evidence")
                    and node.get("eval_score") is None):
                yield node, path
            return
        for k, v in node.items():
            yield from leaves_with_evidence(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from leaves_with_evidence(item, f"{path}[{i}]")


def chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


BATCH_PROMPT = """You are scoring FAITHFULNESS: for each numbered item below, \
is the "value" directly stated or clearly entailed by its "evidence" \
snippet? Not real-world correctness — just whether the evidence actually \
supports the value. Score 0.0 (contradicted / unsupported) to 1.0 \
(fully supported). If value and evidence are the same text or evidence \
plainly states the value, that is a 1.0 — do not penalize exact or \
near-exact matches.

Return ONLY a JSON array with EXACTLY {n} objects, one per item below, in \
the SAME ORDER as the items, no other text. Each object must be \
{{"id": <the item's number>, "field": <copy the item's field name exactly>, \
"score": <number 0.0-1.0>}}. Do not skip, merge, or reorder items.

Items:
{items_block}

JSON array:"""


def build_batch_prompt(batch):
    lines = []
    for i, (leaf, field_path) in enumerate(batch):
        field_name = field_path.rsplit(".", 1)[-1]
        lines.append(
            f'{i}. field="{field_name}" value={json.dumps(leaf["value"])} '
            f'evidence={json.dumps(str(leaf["evidence"])[:1500])}'
        )
    return BATCH_PROMPT.format(n=len(batch), items_block="\n".join(lines))


def score_batch(batch, section_label, batch_num, total_batches):
    """One call_llm call scores every leaf in `batch`. Falls back to
    leaving eval_score=None for the whole batch on unparseable JSON —
    same 'not wrong, just unscored' semantics as a single-leaf failure
    in the old version, just batched.

    Each response entry must echo the field name it judged — if that
    doesn't match what THIS id's leaf actually is, the model's response
    has drifted (dropped/reordered an item) and trusting the score would
    silently attach it to the wrong leaf (confirmed failure mode: exact-
    match value/evidence pairs scoring 0.0). Mismatches are left unscored
    and reported instead of silently accepted."""
    prompt = build_batch_prompt(batch)
    raw = call_llm(prompt)
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        scores = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[warn] {section_label} batch {batch_num}/{total_batches}: "
              f"unparseable judge response ({e}) — leaving this batch unscored")
        return

    by_id = {}
    for s in scores:
        if "id" in s:
            try:
                by_id[int(s["id"])] = s
            except (TypeError, ValueError):
                pass

    mismatches = 0
    for i, (leaf, field_path) in enumerate(batch):
        expected_field = field_path.rsplit(".", 1)[-1]
        entry = by_id.get(i)
        if entry is None:
            leaf["eval_score"] = None
            continue
        returned_field = entry.get("field")
        if returned_field != expected_field:
            # response drifted — this id's score belongs to a different
            # item than the one actually at this position. Don't guess.
            mismatches += 1
            leaf["eval_score"] = None
            continue
        score = entry.get("score")
        try:
            leaf["eval_score"] = round(float(score), 4) if score is not None else None
        except (TypeError, ValueError):
            leaf["eval_score"] = None

    status = f"batch {batch_num}/{total_batches} scored ({len(batch)} leaves)"
    if mismatches:
        status += f" — {mismatches} id/field mismatch(es), left unscored"
    print(f"  {section_label}: {status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("year")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()
    ticker, year = args.ticker.upper(), args.year

    path = os.path.join(FINAL_DIR, ticker, year, "template.json")
    if not os.path.exists(path):
        raise SystemExit(f"Run the pipeline through analysis.py first — {path} missing")
    with open(path, encoding="utf-8") as f:
        template = json.load(f)

    quota_hit = False
    for section in EVAL_SECTIONS:
        if section not in template:
            continue
        leaves = list(leaves_with_evidence(template[section], section))
        if not leaves:
            continue
        batches = list(chunk(leaves, args.batch_size))
        print(f"Evaluating {section}: {len(leaves)} leaves with value+evidence "
              f"in {len(batches)} batch(es) of up to {args.batch_size}...")
        try:
            for i, batch in enumerate(batches, 1):
                score_batch(batch, section, i, len(batches))
        except GeminiQuotaExhausted:
            quota_hit = True
            print(f"\n[warn] Gemini quota exhausted mid-{section} — stopping here. "
                  f"Every score computed so far is kept; the rest stays unscored "
                  f"(not wrong, just not yet checked — re-run this script later "
                  f"once quota resets, it skips leaves that already have a score).")
            break

    # recompute totals fresh from the saved template rather than trusting
    # counters carried through the loop above — correct whether this run
    # finished cleanly or was cut short by quota_hit.
    all_leaves = []
    for section in EVAL_SECTIONS:
        if section in template:
            all_leaves.extend(walk_all_scorable(template[section], section))
    scored = [(l, p) for l, p in all_leaves if l.get("eval_score") is not None]
    flagged = [(l, p) for l, p in scored if l["eval_score"] < THRESHOLD]
    for leaf, field_path in sorted(flagged, key=lambda lp: lp[0]["eval_score"]):
        print(f"  [flag {leaf['eval_score']}] {field_path}")

    if not scored:
        print("[warn] nothing scored this run (quota exhausted immediately, "
              "or nothing left to evaluate). Nothing saved.")
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    print(f"\nDone: {len(scored)} leaves scored total, {len(flagged)} below threshold ({THRESHOLD}).")
    if quota_hit:
        unscored = len(all_leaves) - len(scored)
        print(f"{unscored} leaves still unscored (quota exhausted) — re-run "
              f"`python evaluate.py {ticker} {year}` after your quota resets "
              f"to fill in the rest; already-scored leaves won't be re-billed.")
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()