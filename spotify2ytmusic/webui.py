#!/usr/bin/env python3
"""
Web UI for Spotify to YT Music. Run with: python -m spotify2ytmusic webui
"""
import json
import os
import subprocess
import sys
import threading

# Optional Flask; we check at runtime so tk-based gui remains usable without flask
try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError:
    Flask = None

# Project root: set in main() to cwd so oauth.json/playlists.json are found
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(ROOT, "settings.json")

# Single job state (one task at a time)
_job = {"running": False, "log": [], "returncode": None, "task": None}
_job_lock = threading.Lock()


def _get_algo():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return int(json.load(f).get("algo_number", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return 0


def _save_settings(algo_number):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"auto_scroll": True, "algo_number": algo_number}, f, indent=2)
    except OSError:
        pass


def _run_task(task, spotify_id="", yt_id="", algo=None):
    if algo is None:
        algo = _get_algo()
    algo_arg = str(algo)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    cwd = ROOT
    python = sys.executable
    cmd_base = [python, "-m", "spotify2ytmusic"]

    if task == "backup":
        cmd = cmd_base + ["backup"]
    elif task == "load_liked":
        cmd = cmd_base + ["load_liked", "--algo", algo_arg]
    elif task == "list_playlists":
        cmd = cmd_base + ["list_playlists"]
    elif task == "copy_all":
        cmd = cmd_base + ["copy_all_playlists", "--algo", algo_arg]
    elif task == "copy_playlist":
        if not spotify_id or not yt_id:
            with _job_lock:
                _job["log"].append("Error: Spotify playlist ID and YT Music playlist ID required.")
                _job["returncode"] = 1
                _job["running"] = False
            return
        cmd = cmd_base + ["copy_playlist", spotify_id, yt_id, "--algo", algo_arg]
    else:
        with _job_lock:
            _job["log"].append(f"Unknown task: {task}")
            _job["returncode"] = 1
            _job["running"] = False
        return

    with _job_lock:
        _job["log"] = []
        _job["returncode"] = None
        _job["task"] = task
        _job["running"] = True

    def run():
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            for line in proc.stdout:
                with _job_lock:
                    _job["log"].append(line.rstrip())
            proc.wait()
            with _job_lock:
                _job["returncode"] = proc.returncode
                _job["running"] = False
        except Exception as e:
            with _job_lock:
                _job["log"].append(str(e))
                _job["returncode"] = 1
                _job["running"] = False

    threading.Thread(target=run, daemon=True).start()


def create_app():
    if Flask is None:
        raise RuntimeError("Install Flask to use the web UI: pip install flask")

    app = Flask(__name__, static_folder=None)

    @app.route("/")
    def index():
        return _html()

    @app.route("/api/status")
    def status():
        with _job_lock:
            return jsonify(
                {
                    "running": _job["running"],
                    "log": "\n".join(_job["log"]),
                    "returncode": _job["returncode"],
                    "task": _job["task"],
                }
            )

    @app.route("/api/run", methods=["POST"])
    def run():
        data = request.get_json(force=True, silent=True) or {}
        task = data.get("task", "")
        spotify_id = data.get("spotify_id", "").strip()
        yt_id = data.get("yt_id", "").strip()
        algo = data.get("algo")
        if algo is not None:
            try:
                algo = int(algo)
            except (TypeError, ValueError):
                algo = None
        with _job_lock:
            if _job["running"]:
                return jsonify({"ok": False, "error": "A task is already running"}), 409
        if task not in ("backup", "load_liked", "list_playlists", "copy_all", "copy_playlist"):
            return jsonify({"ok": False, "error": "Invalid task"}), 400
        _run_task(task, spotify_id=spotify_id, yt_id=yt_id, algo=algo)
        return jsonify({"ok": True, "job_id": "current"})

    @app.route("/api/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "GET":
            return jsonify({"algo_number": _get_algo()})
        data = request.get_json(force=True, silent=True) or {}
        algo = data.get("algo_number", data.get("algo"))
        if algo is not None:
            try:
                algo = int(algo)
                if 0 <= algo <= 2:
                    _save_settings(algo)
            except (TypeError, ValueError):
                pass
        return jsonify({"algo_number": _get_algo()})

    @app.route("/api/yt_login_required")
    def yt_login_required():
        return jsonify({"required": not os.path.exists(os.path.join(ROOT, "oauth.json"))})

    @app.route("/api/credentials")
    def credentials():
        """Check if oauth.json exists and if OAuth client credentials are available (env, ytmusic_client.json, or default_ytmusic_client.json)."""
        from . import backend
        oauth_path = os.path.join(ROOT, "oauth.json")
        client_id, client_secret = backend.get_oauth_client_id_secret()
        has_client = bool(client_id and client_secret)
        return jsonify({
            "has_oauth": os.path.isfile(oauth_path),
            "has_ytmusic_client": has_client,
            "project_root": ROOT,
        })

    return app


def _html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spotify → YT Music</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,600;1,9..40,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0d0d0f;
      --surface: #161618;
      --surface2: #1c1c1f;
      --border: #2a2a2e;
      --text: #e4e4e7;
      --textMuted: #71717a;
      --accent: #22c55e;
      --accentDim: #16a34a;
      --danger: #ef4444;
      --radius: 12px;
      --radiusSm: 8px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: 'DM Sans', system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.5;
    }
    .layout {
      max-width: 900px;
      margin: 0 auto;
      padding: 2rem 1.5rem;
    }
    h1 {
      font-size: 1.75rem;
      font-weight: 600;
      margin: 0 0 0.25rem 0;
      letter-spacing: -0.02em;
    }
    .subtitle {
      color: var(--textMuted);
      margin-bottom: 2rem;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.25rem 1.5rem;
      margin-bottom: 1rem;
    }
    .card h2 {
      font-size: 0.95rem;
      font-weight: 600;
      margin: 0 0 0.75rem 0;
      color: var(--textMuted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .card p {
      margin: 0 0 1rem 0;
      color: var(--text);
    }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.6rem 1.1rem;
      font-family: inherit;
      font-size: 0.9rem;
      font-weight: 600;
      color: #fff;
      background: var(--accent);
      border: none;
      border-radius: var(--radiusSm);
      cursor: pointer;
      transition: background 0.15s;
    }
    .btn:hover:not(:disabled) { background: var(--accentDim); }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; }
    .btn.secondary {
      background: var(--surface2);
      color: var(--text);
      border: 1px solid var(--border);
    }
    .btn.secondary:hover:not(:disabled) { background: var(--border); }
    .form-row {
      display: flex;
      gap: 0.75rem;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 0.75rem;
    }
    .form-row label { min-width: 140px; color: var(--textMuted); font-size: 0.9rem; }
    input[type="text"], input[type="number"] {
      flex: 1;
      min-width: 160px;
      padding: 0.5rem 0.75rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85rem;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: var(--radiusSm);
      color: var(--text);
    }
    input:focus { outline: none; border-color: var(--accent); }
    .log-box {
      background: #0a0a0b;
      border: 1px solid var(--border);
      border-radius: var(--radiusSm);
      padding: 1rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.8rem;
      line-height: 1.5;
      color: var(--textMuted);
      white-space: pre-wrap;
      word-break: break-all;
      max-height: 320px;
      overflow-y: auto;
      margin-top: 1rem;
    }
    .log-box:empty::before { content: 'Log output will appear here…'; opacity: 0.6; }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.85rem;
      margin-left: 0.5rem;
    }
    .status.running { color: var(--accent); }
    .status.done { color: var(--textMuted); }
    .status.error { color: var(--danger); }
    .algo-select {
      padding: 0.4rem 0.6rem;
      font-family: inherit;
      font-size: 0.9rem;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: var(--radiusSm);
      color: var(--text);
      cursor: pointer;
    }
    footer {
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid var(--border);
      font-size: 0.85rem;
      color: var(--textMuted);
    }
    footer a { color: var(--accent); text-decoration: none; }
    .warning-card {
      border-color: var(--danger);
      background: rgba(239, 68, 68, 0.08);
    }
    .warning-card h2 { color: var(--danger); }
    .warning-card code { background: var(--surface2); padding: 0.2em 0.4em; border-radius: 4px; }
  </style>
</head>
<body>
  <div class="layout">
    <h1>Spotify → YT Music</h1>
    <p class="subtitle">Backup playlists and liked songs, then copy them to YouTube Music.</p>

    <div class="card" id="credentials-warning" style="display: none;">
      <h2>OAuth client ID and secret needed</h2>
      <p>YT Music OAuth requires a client ID and secret. If the project does not ship a default, create a file <strong>ytmusic_client.json</strong> in the project directory:</p>
      <pre style="background: var(--surface2); padding: 1rem; border-radius: var(--radiusSm); overflow-x: auto; font-size: 0.85rem;">{
  "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "YOUR_CLIENT_SECRET"
}</pre>
      <p>Get the values from <a href="https://developers.google.com/youtube/registering_an_application" target="_blank" rel="noopener">Google Cloud Console</a>: create an OAuth client ID, type <strong>TVs and Limited Input devices</strong>. When your login expires, run <code>python -m spotify2ytmusic ytoauth</code> again to log in with Google; you do not need new client ID/secret.</p>
    </div>

    <div class="card">
      <h2>1. YouTube Music</h2>
      <p>Ensure you are logged in (oauth.json exists). If not, run in terminal: <code>python -m spotify2ytmusic ytoauth</code></p>
    </div>

    <div class="card">
      <h2>2. Spotify backup</h2>
      <p>Export your Spotify playlists and liked songs to <code>playlists.json</code>. A browser may open to log in to Spotify.</p>
      <button type="button" class="btn" id="btn-backup">Backup Spotify</button>
      <span class="status" id="status-backup"></span>
    </div>

    <div class="card">
      <h2>3. Load liked songs</h2>
      <p>Copy your Spotify “Liked Songs” to YT Music liked tracks.</p>
      <button type="button" class="btn" id="btn-load-liked">Load liked songs</button>
      <span class="status" id="status-load-liked"></span>
    </div>

    <div class="card">
      <h2>4. List playlists</h2>
      <p>Show playlist IDs for Spotify and YT Music (useful for the next steps).</p>
      <button type="button" class="btn" id="btn-list">List playlists</button>
      <span class="status" id="status-list"></span>
    </div>

    <div class="card">
      <h2>5. Copy all playlists</h2>
      <p>Copy all Spotify playlists to YT Music (except Liked Songs). Can take a while.</p>
      <button type="button" class="btn" id="btn-copy-all">Copy all playlists</button>
      <span class="status" id="status-copy-all"></span>
    </div>

    <div class="card">
      <h2>6. Copy one playlist</h2>
      <p>Copy a single playlist. Use IDs from “List playlists”. For a new YT playlist use <code>+Name</code>.</p>
      <div class="form-row">
        <label>Spotify playlist ID</label>
        <input type="text" id="input-spotify-id" placeholder="e.g. 3abc…">
      </div>
      <div class="form-row">
        <label>YT Music playlist ID or +Name</label>
        <input type="text" id="input-yt-id" placeholder="e.g. PLxxx… or +My Playlist">
      </div>
      <button type="button" class="btn" id="btn-copy-one">Copy playlist</button>
      <span class="status" id="status-copy-one"></span>
    </div>

    <div class="card">
      <h2>Settings</h2>
      <p>Search algorithm: exact match (0), extended (1), or approximate with videos (2).</p>
      <select class="algo-select" id="algo">
        <option value="0">0 – Exact match</option>
        <option value="1">1 – Extended match</option>
        <option value="2">2 – Approximate + videos</option>
      </select>
      <button type="button" class="btn secondary" id="btn-save-settings">Save</button>
    </div>

    <div class="card">
      <h2>Log</h2>
      <pre class="log-box" id="log"></pre>
    </div>

    <footer>
      Run from project directory so <code>oauth.json</code> and <code>playlists.json</code> are in the right place.
    </footer>
  </div>

  <script>
    const logEl = document.getElementById('log');
    const statusEls = {
      backup: document.getElementById('status-backup'),
      load_liked: document.getElementById('status-load-liked'),
      list: document.getElementById('status-list'),
      copy_all: document.getElementById('status-copy-all'),
      copy_playlist: document.getElementById('status-copy-one'),
    };

    function setStatus(task, state, text) {
      const el = statusEls[task];
      if (!el) return;
      el.textContent = text;
      el.className = 'status ' + state;
    }

    function clearStatuses() {
      Object.keys(statusEls).forEach(k => setStatus(k, 'done', ''));
    }

    function poll() {
      fetch('/api/status')
        .then(r => r.json())
        .then(data => {
          logEl.textContent = data.log || '';
          if (data.log) logEl.scrollTop = logEl.scrollHeight;
          if (data.running) {
            setTimeout(poll, 500);
            document.querySelectorAll('.btn').forEach(b => b.disabled = true);
            return;
          }
          document.querySelectorAll('.btn').forEach(b => b.disabled = false);
          if (data.task && data.returncode !== null) {
            const st = data.returncode === 0 ? 'done' : 'error';
            const txt = data.returncode === 0 ? 'Done' : 'Failed';
            setStatus(data.task, st, txt);
          }
        })
        .catch(() => setTimeout(poll, 1000));
    }

    function runTask(task, body = {}) {
      clearStatuses();
      setStatus(task, 'running', 'Running…');
      fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task, ...body, algo: parseInt(document.getElementById('algo').value, 10) }),
      })
        .then(r => r.json())
        .then(data => {
          if (!data.ok) {
            setStatus(task, 'error', data.error || 'Error');
            return;
          }
          poll();
        })
        .catch(() => setStatus(task, 'error', 'Request failed'));
    }

    document.getElementById('btn-backup').onclick = () => runTask('backup');
    document.getElementById('btn-load-liked').onclick = () => runTask('load_liked');
    document.getElementById('btn-list').onclick = () => runTask('list_playlists');
    document.getElementById('btn-copy-all').onclick = () => runTask('copy_all');
    document.getElementById('btn-copy-one').onclick = () => {
      const sid = document.getElementById('input-spotify-id').value.trim();
      const yid = document.getElementById('input-yt-id').value.trim();
      runTask('copy_playlist', { spotify_id: sid, yt_id: yid });
    };

    document.getElementById('btn-save-settings').onclick = () => {
      fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ algo_number: parseInt(document.getElementById('algo').value, 10) }),
      }).then(() => {});
    };

    fetch('/api/settings').then(r => r.json()).then(s => {
      const algo = document.getElementById('algo');
      if (s.algo_number >= 0 && s.algo_number <= 2) algo.value = String(s.algo_number);
    });
    fetch('/api/credentials').then(r => r.json()).then(d => {
      if (!d.has_ytmusic_client) document.getElementById('credentials-warning').style.display = 'block';
    });
    poll();
  </script>
</body>
</html>"""


def main():
    global ROOT, SETTINGS_FILE
    ROOT = os.getcwd()
    SETTINGS_FILE = os.path.join(ROOT, "settings.json")
    if Flask is None:
        print("Install Flask to use the web UI: pip install flask", file=sys.stderr)
        sys.exit(1)
    app = create_app()
    print("Web UI: http://127.0.0.1:5000")
    print("Press Ctrl+C to stop.")
    app.run(host="127.0.0.1", port=5000, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
