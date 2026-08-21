"""
run_pipeline.py — runs the full pipeline in order, one command instead of six.
Streams each script's output live as it prints (no buffering, no reordering).
Stops immediately if any stage fails — later stages need earlier ones' output.
PPT stage is non-fatal — a bad photo URL or missing node won't kill a
finished template.json.

Usage:
    python run_pipeline.py "Apple" --year 2025
    python run_pipeline.py "Apple" --year 2025 --no-photos   # skip photo fetch
    (company search/confirm still happens interactively inside fetch.py)

After fetch.py, ticker+year are read back from the saved metadata.json so
you don't have to type them again for the remaining stages.
"""

import argparse
import glob
import json
import os
import subprocess
import sys


def run_step(cmd):
    print(f"\n=== RUNNING: {' '.join(cmd)} ===", flush=True)
    # no capture_output — child's stdout/stderr go straight to this terminal,
    # live, in original order, including any interactive input() prompts.
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n=== FAILED: {' '.join(cmd)} (exit {result.returncode}) ===")
        sys.exit(result.returncode)


def run_step_optional(cmd):
    """Like run_step but never aborts the pipeline — for stages that can
    fail (dead photo URL, no internet, no node) without invalidating a
    template.json that's already been written to disk."""
    print(f"\n=== RUNNING (optional): {' '.join(cmd)} ===", flush=True)
    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"\n=== SKIPPED (non-fatal): {' '.join(cmd)} (exit {result.returncode}) ===")
    except FileNotFoundError as e:
        print(f"\n=== SKIPPED (non-fatal): {' '.join(cmd)} ({e}) ===")


def find_latest_metadata(company_hint):
    # fetch.py just ran; find the metadata.json it wrote, most-recently-modified
    candidates = glob.glob("raw/US/*/*/metadata.json")
    if not candidates:
        sys.exit("No metadata.json found after fetch — fetch step must have failed silently.")
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    with open(candidates[0]) as f:
        meta = json.load(f)
    return meta["ticker"], meta["filing_date"][:4]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("company_name")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--no-photos", action="store_true", help="skip leadership photo download")
    parser.add_argument("--no-ppt", action="store_true", help="skip PPT generation")
    args = parser.parse_args()

    fetch_cmd = [sys.executable, "fetch.py", args.company_name]
    if args.year:
        fetch_cmd += ["--year", str(args.year)]
    run_step(fetch_cmd)

    ticker, year = find_latest_metadata(args.company_name)
    print(f"\n>>> Using ticker={ticker} year={year} for remaining stages")

    run_step([sys.executable, "extract_annual_report.py", ticker, year])
    run_step([sys.executable, "extract_lists.py", ticker, year])
    run_step([sys.executable, "derive.py", ticker, year])
    run_step([sys.executable, "enrich.py", ticker, year])

    # quarterly — reuses the same CIK/ticker fetch.py already resolved,
    # skips the interactive company picker the second time
    metadata_path = f"raw/US/{ticker}/{year}/metadata.json"
    with open(metadata_path) as f:
        cik = json.load(f)["cik"]
    run_step([sys.executable, "fetch.py", args.company_name, "--form", "10-Q", "--cik", str(cik), "--ticker", ticker, "--year", year])
    run_step([sys.executable, "quarterly.py", ticker, year])

    run_step([sys.executable, "analysis.py", ticker, year])

    final_json = f"final/US/{ticker}/{year}/template.json"
    print(f"\n=== DONE — {final_json} ===")

    if not args.no_ppt:
        slide_data = f"final/US/{ticker}/{year}/slide_data.json"
        deck = f"final/US/{ticker}/{year}/{ticker}_deck.pptx"

        run_step([sys.executable, "extract_slide_data.py", final_json, slide_data])
        if not args.no_photos:
            run_step_optional([sys.executable, "fetch_photos.py", slide_data])
        run_step_optional(["node", "build_ppt.js", slide_data, deck])

        print(f"\n=== PPT — {deck} ===" if os.path.exists(deck) else "\n=== PPT build skipped/failed — template.json still saved above ===")


if __name__ == "__main__":
    main()
