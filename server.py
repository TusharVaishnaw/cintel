"""
server.py — local dashboard backend for run_pipeline.py.

Lives in the project root, next to fetch.py, run_pipeline.py, etc. Run:

    pip install flask python-dotenv --break-system-packages
    python server.py

Then open http://localhost:5050 — pick a company, watch stages run live,
see logs, DeepEval flags, download the finished deck, browse the JSON.

Design:
- POST /api/search resolves the company name to real candidates FIRST
  (reuses fetch.py's own search_company()), so the user picks a real
  company in the UI instead of run_pipeline.py's blocking terminal
  input(). The chosen cik/ticker is then passed straight to
  run_pipeline.py's --cik/--ticker flags — the whole run is
  non-interactive end to end, no subprocess stdin plumbing needed.
- Each run is a subprocess of run_pipeline.py. A reader thread tails its
  stdout line-by-line, parses run_pipeline.py's "### STAGE x ###" /
  "### DONE ... ###" markers (fixed, script-emitted — not fragile text
  matching on the human-readable log lines, which change wording per
  run: 10-K vs 10-Q fetch, different ticker/year every time).
- Every log line + stage transition is pushed to per-run subscriber
  queues and served over Server-Sent Events (SSE) — reconnect-safe: a
  late subscriber first gets the full buffered log, then live lines.
- Completed runs get appended to webapp_run_history.json (rolling last
  30) — average total/per-stage seconds from that file drives the
  progress bar's ETA on the NEXT run. First-ever run has no ETA yet;
  the bar still shows elapsed time.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import queue
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify, Response, send_file, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv()  # picks up .env from cwd (project root) — GEMINI_API_KEY etc.
except ImportError:
    print("[warn] python-dotenv not installed — .env won't be auto-loaded "
          "(pip install python-dotenv --break-system-packages), falling back "
          "to whatever's already in the shell environment")

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))  # server.py lives in project root
sys.path.insert(0, PIPELINE_DIR)  # so `from fetch import search_company` resolves

HISTORY_PATH = os.path.join(PIPELINE_DIR, "webapp_run_history.json")
HISTORY_CAP = 30

STAGE_SEQUENCE = [
    "fetch_annual", "extract_annual_report", "extract_lists", "derive",
    "enrich", "fetch_quarterly", "quarterly", "analysis",
    "extract_slide_data", "fetch_photos", "build_ppt", "evaluate",
]
STAGE_LABELS = {
    "fetch_annual": "Fetch 10-K (SEC EDGAR)",
    "extract_annual_report": "Extract annual report (XBRL + LLM)",
    "extract_lists": "Extract list fields",
    "derive": "Derive financial ratios",
    "enrich": "External enrichment",
    "fetch_quarterly": "Fetch 10-Q (SEC EDGAR)",
    "quarterly": "Quarterly financials",
    "analysis": "SWOT / industry synthesis",
    "evaluate": "DeepEval faithfulness check",
    "extract_slide_data": "Flatten slide data",
    "fetch_photos": "Fetch leadership photos",
    "build_ppt": "Build PowerPoint",
}

STATIC_DIR = os.path.join(PIPELINE_DIR, "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")

RUNS = {}  # run_id -> run state dict
RUNS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# run history (for ETA)
# ---------------------------------------------------------------------------
def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history_entry(entry):
    hist = load_history()
    hist.append(entry)
    hist = hist[-HISTORY_CAP:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)


def history_averages():
    hist = load_history()
    if not hist:
        return {"runs": 0, "avg_total": None, "avg_stage": {}}
    avg_total = sum(h["total_seconds"] for h in hist) / len(hist)
    stage_sums, stage_counts = {}, {}
    for h in hist:
        for sid, secs in h.get("stage_seconds", {}).items():
            stage_sums[sid] = stage_sums.get(sid, 0) + secs
            stage_counts[sid] = stage_counts.get(sid, 0) + 1
    avg_stage = {sid: stage_sums[sid] / stage_counts[sid] for sid in stage_sums}
    return {"runs": len(hist), "avg_total": avg_total, "avg_stage": avg_stage}


# ---------------------------------------------------------------------------
# run lifecycle
# ---------------------------------------------------------------------------
def broadcast(run, event):
    with run["lock"]:
        for q in run["subscribers"]:
            q.put(event)


def reader_thread(run_id):
    run = RUNS[run_id]
    proc = run["process"]
    current_stage = None
    stage_start = run["start_time"]

    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        ts = time.time()
        run["log"].append(line)

        if line.startswith("### STAGE "):
            stage_id = line.split("### STAGE ", 1)[1].split(" ###")[0].strip()
            if current_stage is not None:
                run["stage_seconds"][current_stage] = round(ts - stage_start, 2)
            current_stage = stage_id
            stage_start = ts
            run["current_stage"] = stage_id
            broadcast(run, {"type": "stage", "stage": stage_id, "label": STAGE_LABELS.get(stage_id, stage_id),
                             "elapsed": round(ts - run["start_time"], 2)})
            continue

        if line.startswith("### DONE "):
            if current_stage is not None:
                run["stage_seconds"][current_stage] = round(ts - stage_start, 2)
            parts = dict(
                p.split("=", 1) for p in line.replace("### DONE ", "").replace(" ###", "").split(" ") if "=" in p
            )
            run["ticker"] = parts.get("ticker")
            run["year"] = parts.get("year")
            run["deck_path"] = parts.get("deck") or None
            continue

        broadcast(run, {"type": "log", "line": line, "elapsed": round(ts - run["start_time"], 2)})

    proc.wait()
    end_time = time.time()
    run["end_time"] = end_time
    run["returncode"] = proc.returncode
    if run["status"] == "stopped":
        return  # killed via /api/stop — don't relabel as error
    run["status"] = "done" if proc.returncode == 0 else "error"

    if run["status"] == "done":
        save_history_entry({
            "ticker": run.get("ticker"), "year": run.get("year"),
            "total_seconds": round(end_time - run["start_time"], 2),
            "stage_seconds": run["stage_seconds"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    broadcast(run, {"type": "done", "status": run["status"], "returncode": proc.returncode,
                     "ticker": run.get("ticker"), "year": run.get("year"),
                     "deck_ready": bool(run.get("deck_path")),
                     "total_seconds": round(end_time - run["start_time"], 2)})


@app.route("/api/search")
def api_search():
    company = request.args.get("company", "").strip()
    if not company:
        return jsonify({"error": "company required"}), 400
    from fetch import search_company
    try:
        candidates = search_company(company, limit=5)
    except SystemExit as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"candidates": [
        {"title": c["title"], "ticker": c["ticker"], "cik": c["cik_str"]} for c in candidates
    ]})


@app.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(force=True)
    company = body.get("company", "").strip()
    year = body.get("year")
    cik = body.get("cik")
    ticker = body.get("ticker")
    no_photos = bool(body.get("no_photos"))
    no_ppt = bool(body.get("no_ppt"))
    if not company or not cik or not ticker:
        return jsonify({"error": "company, cik, ticker required — call /api/search first"}), 400

    run_id = uuid.uuid4().hex[:12]
    cmd = [sys.executable, "run_pipeline.py", company, "--cik", str(cik), "--ticker", ticker]
    if year:
        cmd += ["--year", str(year)]
    if no_photos:
        cmd.append("--no-photos")
    if no_ppt:
        cmd.append("--no-ppt")

    # run_pipeline.py spawns its own child (fetch.py, extract_annual_report.py,
    # etc via subprocess.run) — on POSIX, start_new_session=True puts the whole
    # tree in its own process group so os.killpg can take out parent + child
    # together; plain proc.kill() only kills run_pipeline.py itself and can
    # orphan a still-running LLM call underneath it.
    popen_kwargs = dict(
        cwd=PIPELINE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, universal_newlines=True,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    run = {
        "id": run_id, "company": company, "cik": cik, "ticker": ticker, "year": year,
        "process": proc, "log": [], "subscribers": [], "lock": threading.Lock(),
        "status": "running", "current_stage": STAGE_SEQUENCE[0],
        "start_time": time.time(), "end_time": None, "stage_seconds": {},
        "deck_path": None, "returncode": None,
    }
    with RUNS_LOCK:
        RUNS[run_id] = run
    threading.Thread(target=reader_thread, args=(run_id,), daemon=True).start()
    return jsonify({"run_id": run_id, "stages": [
        {"id": s, "label": STAGE_LABELS[s]} for s in STAGE_SEQUENCE
    ]})


@app.route("/api/stop/<run_id>", methods=["POST"])
def api_stop(run_id):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "unknown run_id"}), 404
    if run["status"] != "running":
        return jsonify({"status": run["status"], "message": "already finished"}), 200

    proc = run["process"]
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        print(f"[warn] stop {run_id}: process already gone ({e})")

    run["status"] = "stopped"
    run["end_time"] = time.time()
    broadcast(run, {"type": "done", "status": "stopped", "returncode": None,
                     "ticker": run.get("ticker"), "year": run.get("year"),
                     "deck_ready": bool(run.get("deck_path")),
                     "total_seconds": round(run["end_time"] - run["start_time"], 2)})
    return jsonify({"status": "stopped"})


@app.route("/api/stream/<run_id>")
def api_stream(run_id):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "unknown run_id"}), 404

    def gen():
        q = queue.Queue()
        with run["lock"]:
            run["subscribers"].append(q)
        # catch a late subscriber up on everything so far
        for line in run["log"]:
            yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
        # Confirmed bug: a client that (re)connects mid-run only ever got a
        # single 'stage' event for the CURRENT stage — never the sequence of
        # transitions that already happened. If a browser's EventSource
        # reconnected between two stage transitions (tab backgrounded, brief
        # network hiccup), the stage(s) that completed before it reconnected
        # never got marked "done" client-side — permanently stuck grey even
        # though the pipeline genuinely finished them. run["stage_seconds"]
        # already records every stage that has completed (see reader_thread);
        # replay those as synthetic 'stage-done' backfill events before the
        # live 'stage' event for whatever's currently running, so the client
        # can catch up regardless of when it (re)connected.
        for done_stage_id, secs in run["stage_seconds"].items():
            yield f"data: {json.dumps({'type': 'stage-done', 'stage': done_stage_id, 'elapsed_seconds': secs})}\n\n"
        yield f"data: {json.dumps({'type': 'stage', 'stage': run['current_stage'], 'label': STAGE_LABELS.get(run['current_stage'], run['current_stage'])})}\n\n"
        if run["status"] != "running":
            yield f"data: {json.dumps({'type': 'done', 'status': run['status'], 'deck_ready': bool(run.get('deck_path'))})}\n\n"
            return
        try:
            while True:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
        except queue.Empty:
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            with run["lock"]:
                if q in run["subscribers"]:
                    run["subscribers"].remove(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.route("/api/status/<run_id>")
def api_status(run_id):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "unknown run_id"}), 404
    return jsonify({
        "status": run["status"], "current_stage": run["current_stage"],
        "stage_seconds": run["stage_seconds"], "ticker": run.get("ticker"),
        "year": run.get("year"), "deck_ready": bool(run.get("deck_path")),
        "elapsed": round((run["end_time"] or time.time()) - run["start_time"], 2),
    })


@app.route("/api/history")
def api_history():
    return jsonify(history_averages())


def _final_paths(run_id):
    run = RUNS.get(run_id)
    if not run or not run.get("ticker") or not run.get("year"):
        return None, None, None
    base = os.path.join(PIPELINE_DIR, "final", "US", run["ticker"], run["year"])
    template = os.path.join(base, "template.json")
    deck = os.path.join(base, f"{run['ticker']}_deck.pptx")
    return base, template, deck


@app.route("/api/eval/<run_id>")
def api_eval(run_id):
    _, template_path, _ = _final_paths(run_id)
    if not template_path or not os.path.exists(template_path):
        return jsonify({"error": "template.json not ready yet"}), 404
    with open(template_path, encoding="utf-8") as f:
        template = json.load(f)

    leaves = []

    def walk(node, path=""):
        if isinstance(node, dict):
            if "value" in node and "eval_score" in node:
                leaves.append({
                    "path": path, "value": node.get("value"),
                    "evidence": node.get("evidence"), "score": node.get("eval_score"),
                })
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    for section in ("from_annual_report", "from_quarterly_report", "external_enrichment"):
        if section in template:
            walk(template[section], section)

    scored = [l for l in leaves if l["score"] is not None]
    flagged = [l for l in scored if l["score"] < 0.7]
    scored.sort(key=lambda l: l["score"])
    return jsonify({
        "total_scored": len(scored), "flagged_count": len(flagged),
        "avg_score": round(sum(l["score"] for l in scored) / len(scored), 4) if scored else None,
        "leaves": scored,
    })


@app.route("/api/json/<run_id>")
def api_json(run_id):
    _, template_path, _ = _final_paths(run_id)
    if not template_path or not os.path.exists(template_path):
        return jsonify({"error": "template.json not ready yet"}), 404
    with open(template_path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/download/<run_id>")
def api_download(run_id):
    _, _, deck_path = _final_paths(run_id)
    if not deck_path or not os.path.exists(deck_path):
        return jsonify({"error": "deck not ready yet"}), 404
    return send_file(deck_path, as_attachment=True)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"Pipeline dir: {PIPELINE_DIR}")
    print("Dashboard: http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, threaded=True, debug=False)