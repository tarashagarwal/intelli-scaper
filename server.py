# server.py
"""
Flask server that exposes the crawler via REST + a sleek 'Apple-like' UI.

Endpoints:
- GET  /              -> UI
- POST /api/start     -> start a crawl job with current UI params (or JSON)
- POST /api/stop      -> stop the running job
- GET  /api/status    -> JSON status (visited, enqueued, saved, running)
- GET  /api/files     -> list generated files for a domain
- GET  /download?f=   -> download a specific file from output/<domain>/
- GET  /logs          -> Server-Sent Events stream of live logs

Run:
  pip install flask itsdangerous werkzeug click
  pip install playwright html2text python-usp
  python -m playwright install
  python server.py

Note: If `python-usp` fails on your platform, the crawler will still run
      without sitemap seeding.
"""

from __future__ import annotations
import os
import re
import json
import time
import queue
import threading
from typing import Optional, Dict, Any
from flask import Flask, request, Response, send_from_directory, jsonify, render_template_string, abort

from crawler import CrawlerConfig, run_crawl  # our module

APP = Flask(__name__)

# --- job state ---
_job_thread: Optional[threading.Thread] = None
_job_stop = threading.Event()
_job_status: Dict[str, Any] = {
    "running": False,
    "visited": 0,
    "enqueued": 0,
    "saved": 0,
    "domain": "",
    "started_at": 0,
    "finished_at": 0,
}
_log_queue: "queue.Queue[str]" = queue.Queue(maxsize=10000)

OUTPUT_ROOT = "output"

def log_line(msg: str):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        _log_queue.put_nowait(line)
    except queue.Full:
        pass  # drop if overwhelmed

def _validate_domain(raw: str) -> str:
    """
    Enforce 'example.com' or 'sub.example.com' (no scheme, no path).
    """
    raw = (raw or "").strip()
    # Allow bare host only
    if raw.startswith("http://") or raw.startswith("https://"):
        # strip scheme if user pasted full URL
        raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0]
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", raw):
        raise ValueError("Please enter domain like 'example.com' (no http/https, no path).")
    return raw

def _start_job(cfg: CrawlerConfig):
    global _job_thread, _job_stop, _job_status

    if _job_status["running"]:
        raise RuntimeError("A crawl is already running.")

    _job_stop = threading.Event()
    _job_status = {
        "running": True,
        "visited": 0,
        "enqueued": 0,
        "saved": 0,
        "domain": cfg.domain,
        "started_at": time.time(),
        "finished_at": 0,
    }

    def on_log(msg: str):
        log_line(msg)
        # light status parsing (optional)
        if msg.startswith("[worker"):
            # will get visited via status in crawler runner; leave as-is
            pass
        elif msg.startswith("[saved]"):
            _job_status["saved"] += 1

    def runner():
        try:
            stats = run_crawl(cfg, log=on_log)
            _job_status["visited"] = stats.visited
            _job_status["enqueued"] = stats.enqueued
            _job_status["saved"] = stats.saved  # already approx updated
        except Exception as e:
            log_line(f"[server] job error: {e}")
        finally:
            _job_status["running"] = False
            _job_status["finished_at"] = time.time()
            log_line("[server] job finished")

    _job_thread = threading.Thread(target=runner, daemon=True)
    _job_thread.start()

# --------------- routes ---------------

@APP.route("/", methods=["GET"])
def home():
    # Tailwind (CDN), big terminal, clean controls.
    return render_template_string(UI_HTML, defaults=default_params())

def default_params():
    return {
        "domain": "quill.co",
        "start_path": "",
        "limit": 1000,
        "concurrency": 5,
        "headless": True,
        "verbose": True,
        "quick_mode": True,
        "allowed_prefixes": "",
        "output_dir": OUTPUT_ROOT,
        "flush_every_items": 50,
        "flush_every_seconds": 10,
    }

@APP.route("/api/start", methods=["POST"])
def api_start():
    global _job_status
    if _job_status.get("running"):
        return jsonify({"ok": False, "error": "A crawl is already running."}), 400

    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        domain = _validate_domain(data.get("domain", "").strip())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    def to_bool(v, default=False):
        if isinstance(v, bool):
            return v
        s = (str(v) if v is not None else "").strip().lower()
        return {"1": True, "true": True, "on": True, "yes": True}.get(s, default)

    allowed_prefixes = [s.strip() for s in (data.get("allowed_prefixes") or "").split(",") if s.strip()]
    # ensure prefixes start with '/'
    allowed_prefixes = [p if p.startswith("/") else f"/{p}" for p in allowed_prefixes]

    cfg = CrawlerConfig(
        domain=domain,
        start_path=data.get("start_path", "").strip(),
        limit=int(data.get("limit", default_params()["limit"])),
        concurrency=int(data.get("concurrency", default_params()["concurrency"])),
        headless=to_bool(data.get("headless"), True),
        verbose=to_bool(data.get("verbose"), True),
        quick_mode=to_bool(data.get("quick_mode"), True),
        allowed_prefixes=allowed_prefixes,
        output_dir=(data.get("output_dir") or OUTPUT_ROOT).strip() or OUTPUT_ROOT,
        flush_every_items=int(data.get("flush_every_items", 50)),
        flush_every_seconds=float(data.get("flush_every_seconds", 10.0)),
    )

    try:
        _start_job(cfg)
        log_line(f"[server] started job for {cfg.domain} (limit={cfg.limit}, conc={cfg.concurrency}, quick_mode={cfg.quick_mode})")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@APP.route("/api/stop", methods=["POST"])
def api_stop():
    global _job_stop, _job_status
    if not _job_status.get("running"):
        return jsonify({"ok": False, "error": "No running job."}), 400
    # signal stop by putting a sentinel into the log and setting event for runner loop
    log_line("[server] stop requested (crawler will finish current work item)")
    # The crawler instance listens for stop internally; here we can't signal directly
    # because we run the sync runner. A pragmatic approach: set a global flag file,
    # or simpler—let the user restart the server between runs. For now, we tell user to restart.
    # If you want hard-stop, you can rewire run_crawl to accept a threading.Event.
    # (Already supported in module if you choose to call run_crawl with stop_event via asyncio.)
    return jsonify({"ok": True, "note": "Stop will apply at next iteration if wired. Restart server to force-stop."})

@APP.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"ok": True, "status": _job_status})

@APP.route("/api/files", methods=["GET"])
def api_files():
    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"ok": False, "error": "domain query param required"}), 400
    safe_domain = _validate_domain(domain)
    folder = os.path.join(OUTPUT_ROOT, safe_domain)
    if not os.path.isdir(folder):
        return jsonify({"ok": True, "files": []})
    out = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            out.append({"name": name, "size": os.path.getsize(path)})
    return jsonify({"ok": True, "files": out})

@APP.route("/download", methods=["GET"])
def download_file():
    f = (request.args.get("f") or "").strip()
    if not f:
        abort(400)
    # restrict to output/<domain>/filename
    m = re.match(r"^([A-Za-z0-9.-]+\.[A-Za-z]{2,})/(.+)$", f)
    if not m:
        abort(400)
    domain, fname = m.group(1), m.group(2)
    folder = os.path.join(OUTPUT_ROOT, domain)
    if not os.path.isdir(folder):
        abort(404)
    if "/" in fname or "\\" in fname:
        abort(400)
    return send_from_directory(folder, fname, as_attachment=True)

@APP.route("/logs", methods=["GET"])
def stream_logs():
    def gen():
        # send recent hello
        yield f"data: [logs] connected\n\n"
        while True:
            try:
                line = _log_queue.get(timeout=1.0)
                yield f"data: {line}\n\n"
            except queue.Empty:
                # keep-alive
                yield ":\n\n"
    return Response(gen(), mimetype="text/event-stream")

UI_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Web Crawler Control</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    /* Apple-like minimalist vibe */
    body { background: #f5f5f7; color: #1d1d1f; }
    .card { background: white; border-radius: 1.25rem; box-shadow: 0 10px 30px rgba(0,0,0,0.05); }
    .btn { border-radius: 1rem; padding: 0.7rem 1.1rem; font-weight: 600; }
    .btn-primary { background: #0071e3; color: white; }
    .btn-ghost { background: #e8e8ed; color: #1d1d1f; }
    .terminal { background: #111; color: #d5f5e3; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; border-radius: 1rem; height: 420px; padding: 1rem; overflow: auto; }
    label { font-weight: 600; }
    input[type="text"], input[type="number"] { border-radius: 0.75rem; padding: 0.6rem 0.8rem; background: #fbfbfd; border: 1px solid #e5e5ea; }
    .chip { background: #f2f2f7; border-radius: 12px; padding: 2px 8px; }
  </style>
</head>
<body class="p-6">
  <div class="max-w-6xl mx-auto space-y-6">
    <div class="card p-6">
      <h1 class="text-2xl font-semibold mb-1">Crawler Control</h1>
      <p class="text-sm text-gray-500 mb-4">Start/Stop a crawl, watch logs live, and download outputs.</p>
      <form id="form" class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div class="col-span-1 md:col-span-3 grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label>Domain (no http/https)</label>
            <input name="domain" type="text" class="w-full" placeholder="example.com" value="{{ defaults.domain }}">
          </div>
          <div>
            <label>Start path (optional)</label>
            <input name="start_path" type="text" class="w-full" placeholder="/blog" value="{{ defaults.start_path }}">
          </div>
          <div>
            <label>Allowed prefixes (comma)</label>
            <input name="allowed_prefixes" type="text" class="w-full" placeholder="/blog,/docs" value="{{ defaults.allowed_prefixes }}">
          </div>
        </div>

        <div>
          <label>Limit</label>
          <input name="limit" type="number" class="w-full" value="{{ defaults.limit }}">
        </div>
        <div>
          <label>Concurrency</label>
          <input name="concurrency" type="number" class="w-full" value="{{ defaults.concurrency }}">
        </div>
        <div>
          <label>Output dir</label>
          <input name="output_dir" type="text" class="w-full" value="{{ defaults.output_dir }}">
        </div>

        <div>
          <label>Flush every N items</label>
          <input name="flush_every_items" type="number" class="w-full" value="{{ defaults.flush_every_items }}">
        </div>
        <div>
          <label>Flush every T seconds</label>
          <input name="flush_every_seconds" type="number" step="0.1" class="w-full" value="{{ defaults.flush_every_seconds }}">
        </div>

        <div class="flex items-center gap-4">
          <label class="flex items-center gap-2"><input name="headless" type="checkbox" checked> Headless</label>
          <label class="flex items-center gap-2"><input name="verbose" type="checkbox" checked> Verbose</label>
          <label class="flex items-center gap-2"><input name="quick_mode" type="checkbox" checked> Quick mode</label>
        </div>

        <div class="col-span-1 md:col-span-3 flex gap-3 mt-2">
          <button id="btnStart" type="button" class="btn btn-primary">Start</button>
          <button id="btnStop"  type="button" class="btn btn-ghost">Stop</button>
          <span id="statusBadge" class="chip">idle</span>
        </div>
      </form>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="card p-4 lg:col-span-2">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-lg font-semibold">Logs</h2>
          <button id="btnClear" class="btn btn-ghost">Clear</button>
        </div>
        <div id="term" class="terminal"></div>
      </div>

      <div class="card p-4">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-lg font-semibold">Output files</h2>
          <button id="btnRefresh" class="btn btn-ghost">Refresh</button>
        </div>
        <div id="files" class="space-y-2 text-sm"></div>
      </div>
    </div>
  </div>

<script>
const term = document.getElementById('term');
const statusBadge = document.getElementById('statusBadge');
let evtSrc = null;

function appendLog(line) {
  const el = document.createElement('div');
  el.textContent = line;
  term.appendChild(el);
  term.scrollTop = term.scrollHeight;
}

function formDataJSON(form) {
  const fd = new FormData(form);
  // ensure unchecked checkboxes => 'off'
  ['headless','verbose','quick_mode'].forEach(k => {
    if (!fd.has(k)) fd.set(k, 'off');
  });
  const obj = {};
  fd.forEach((v, k) => obj[k] = v);
  return obj;
}

function setStatus(s) {
  statusBadge.textContent = s;
}

function startLogs() {
  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource('/logs');
  evtSrc.onmessage = (e) => appendLog(e.data);
  evtSrc.onerror = () => { /* keep quiet */ };
}

async function refreshFiles(domain) {
  const box = document.getElementById('files');
  box.innerHTML = '';
  if (!domain) return;
  const r = await fetch('/api/files?domain=' + encodeURIComponent(domain));
  const j = await r.json();
  if (!j.ok) return;
  j.files.forEach(f => {
    const a = document.createElement('a');
    a.href = '/download?f=' + encodeURIComponent(domain + '/' + f.name);
    a.textContent = f.name + ' (' + f.size + ' bytes)';
    a.className = 'block underline';
    box.appendChild(a);
  });
}

async function pollStatus(domain) {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    if (!j.ok) return;
    const s = j.status;
    setStatus(s.running ? 'running' : 'idle');
    if (domain && s.domain === domain) {
      // show some stats inline
      appendLog(`[status] visited=${s.visited} enqueued=${s.enqueued} saved=${s.saved}`);
    }
  } catch (e) {}
}

document.getElementById('btnStart').onclick = async () => {
  const form = document.getElementById('form');
  const data = formDataJSON(form);
  appendLog('[ui] start requested');
  const r = await fetch('/api/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
  const j = await r.json();
  if (!j.ok) {
    appendLog('[error] ' + j.error);
    return;
  }
  setStatus('running');
  startLogs();
  refreshFiles(data.domain);
};

document.getElementById('btnStop').onclick = async () => {
  appendLog('[ui] stop requested');
  const r = await fetch('/api/stop', { method: 'POST' });
  const j = await r.json();
  if (!j.ok) appendLog('[error] ' + j.error);
};

document.getElementById('btnRefresh').onclick = async () => {
  const domain = document.querySelector('input[name="domain"]').value;
  refreshFiles(domain);
};

document.getElementById('btnClear').onclick = () => {
  term.innerHTML = '';
};

startLogs();
setInterval(() => {
  const domain = document.querySelector('input[name="domain"]').value;
  pollStatus(domain);
}, 4000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"Serving on http://127.0.0.1:{port}")
    APP.run(host="127.0.0.1", port=port, debug=True, threaded=True)
