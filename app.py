#!/usr/bin/env python3
"""Web chat interface for Claude Code CLI with mid-stream messaging support."""

import io
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from functools import wraps

from flask import Flask, Response, render_template, request, jsonify, send_file, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ── Secret key (persisted so sessions survive restarts) ──────────────────────
SECRET_KEY_FILE = os.path.expanduser('~/claude-chat/.secret_key')
if os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE) as f:
        app.secret_key = f.read().strip()
else:
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)
    with open(SECRET_KEY_FILE, 'w') as f:
        f.write(app.secret_key)
    os.chmod(SECRET_KEY_FILE, 0o600)

# ── Session cookie hardening ─────────────────────────────────────────────────
app.config['SESSION_COOKIE_SECURE'] = True      # only send over HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True     # no JS access to cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'   # block cross-site POST sends
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 # 24h session timeout

# ── User store ────────────────────────────────────────────────────────────────
USERS_FILE = os.path.expanduser('~/claude-chat/users.json')

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return {}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

SESSION_FILE  = os.path.expanduser('~/claude-chat/.session_id')
USAGE_FILE    = os.path.expanduser('~/claude-chat/.usage_stats.json')
USAGE_SCRIPT  = os.path.expanduser('~/claude-chat/usage_stats.py')
USAGE_MAX_AGE = 120  # seconds before we re-run usage_stats.py
SETTINGS_FILE = os.path.expanduser('~/.claude/settings.json')
DEFAULT_MODEL = 'claude-opus-4-6'


def get_current_model():
    """Read model from Claude Code settings, or return the default."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f).get('model', DEFAULT_MODEL)
    except Exception:
        return DEFAULT_MODEL

# Process management
current_proc = None
proc_lock = threading.Lock()


def load_session():
    """Load persisted session ID, or create a new one."""
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            data = json.load(f)
            return data.get('session_id'), data.get('used', False)
    sid = str(uuid.uuid4())
    save_session(sid, False)
    return sid, False


def save_session(sid, used):
    """Persist session ID to disk."""
    with open(SESSION_FILE, 'w') as f:
        json.dump({'session_id': sid, 'used': used}, f)


session_id, session_used = load_session()


def _tool_summary(tool_name, tool_input):
    """Extract a brief one-line summary from a tool's input dict."""
    if tool_name in ('Read', 'Write'):
        return tool_input.get('file_path', '')
    elif tool_name == 'Edit':
        return tool_input.get('file_path', '')
    elif tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        return cmd[:100] + ('…' if len(cmd) > 100 else '')
    elif tool_name in ('Grep', 'Glob'):
        pat = tool_input.get('pattern', '')
        path = tool_input.get('path', '')
        return f'{pat}' + (f'  in {path}' if path else '')
    elif tool_name == 'Agent':
        return tool_input.get('description', tool_input.get('prompt', '')[:60])
    elif tool_name in ('WebFetch', 'WebSearch'):
        return tool_input.get('url', tool_input.get('query', ''))
    elif tool_name == 'TodoWrite':
        return f"{len(tool_input.get('todos', []))} todo(s)"
    else:
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:100]
        return ''


def kill_current():
    """Kill the currently running claude process, if any."""
    global current_proc
    with proc_lock:
        if current_proc and current_proc.poll() is None:
            current_proc.kill()
            current_proc.wait()
            current_proc = None


# ── Login rate limiting ───────────────────────────────────────────────────────
_login_attempts = {}  # ip -> {'count': int, 'first': timestamp, 'locked_until': timestamp}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 300       # 5 minutes
LOGIN_LOCKOUT = 900      # 15 minute lockout after too many failures


def _get_client_ip():
    return request.headers.get('CF-Connecting-IP',
           request.headers.get('X-Forwarded-For', request.remote_addr))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = _get_client_ip()
        now = time.time()
        info = _login_attempts.get(ip, {})

        # Check if locked out
        if info.get('locked_until', 0) > now:
            remaining = int(info['locked_until'] - now)
            mins = remaining // 60 + 1
            error = f"Too many attempts. Try again in {mins} minute{'s' if mins != 1 else ''}."
            return render_template("login.html", error=error)

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        users = load_users()
        hashed = users.get(username)
        if hashed and check_password_hash(hashed, password):
            _login_attempts.pop(ip, None)
            session.permanent = True
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))

        # Track failed attempt
        if not info or now - info.get('first', 0) > LOGIN_WINDOW:
            info = {'count': 1, 'first': now}
        else:
            info['count'] += 1
        if info['count'] >= LOGIN_MAX_ATTEMPTS:
            info['locked_until'] = now + LOGIN_LOCKOUT
        _login_attempts[ip] = info

        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route("/")
@login_required
def index():
    return render_template("index.html", model=get_current_model())


@app.route("/api/model", methods=["POST"])
@login_required
def api_set_model():
    """Switch the active model in Claude Code settings."""
    data = request.get_json()
    model = data.get("model", "")
    allowed = {"claude-sonnet-4-6", "claude-opus-4-6"}
    if model not in allowed:
        return jsonify({"error": "invalid model"}), 400

    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception:
        settings = {}

    settings["model"] = model
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    return jsonify({"ok": True, "model": model})


@app.route("/api/status")
@login_required
def api_status():
    """Return current session info."""
    busy = current_proc is not None and current_proc.poll() is None
    return jsonify({
        "session_id": session_id[:8] + "…",
        "active": session_used,
        "busy": busy,
    })


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    global session_id, session_used, current_proc

    data = request.get_json()
    message = data.get("message", "")
    if not message.strip():
        return jsonify({"error": "empty message"}), 400

    # Kill any in-flight response so the new message can go through
    kill_current()

    # Keywords that indicate account-level overages / usage limits
    OVERAGE_KEYWORDS = [
        'usage limit', 'over limit', 'exceeded', 'overage',
        'rate limit', 'billing', 'subscription', 'quota',
        'too many requests', '529', '402',
    ]

    def make_cmd():
        c = [
            "claude", "-p",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            message,
        ]
        if session_used:
            c.insert(2, "--resume")
            c.insert(3, session_id)
        else:
            c.insert(2, "--session-id")
            c.insert(3, session_id)
        return c

    def generate():
        global session_used, session_id, current_proc

        for attempt in range(2):
            run_cmd = make_cmd()
            session_expired = False

            proc = subprocess.Popen(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.path.expanduser("~"),
            )
            with proc_lock:
                current_proc = proc

            # Read stderr in a background thread so it doesn't block stdout
            stderr_lines = []
            def read_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line.decode("utf-8", errors="replace").strip())
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()

            # Feed stdout into a queue so we can send keepalives during silent gaps
            stdout_q = queue.Queue()
            def read_stdout():
                for line in proc.stdout:
                    stdout_q.put(line)
                stdout_q.put(None)  # sentinel: stdout closed
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stdout_thread.start()

            KEEPALIVE_INTERVAL = 20  # seconds — well under Cloudflare's ~100s idle timeout

            try:
                while True:
                    try:
                        raw = stdout_q.get(timeout=KEEPALIVE_INTERVAL)
                    except queue.Empty:
                        yield ': keepalive\n\n'
                        continue

                    if raw is None:
                        break  # stdout closed, proc finished

                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield f"data: {json.dumps({'type': 'delta', 'text': text})}\n\n"

                    elif etype == "assistant":
                        msg = event.get("message", {})
                        model = msg.get("model")
                        if model:
                            yield f"data: {json.dumps({'type': 'model', 'model': model})}\n\n"
                        for block in msg.get("content", []):
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text:
                                    yield f"data: {json.dumps({'type': 'delta', 'text': text})}\n\n"
                            elif block.get("type") == "tool_use":
                                tool_name = block.get("name", "unknown")
                                summary = _tool_summary(tool_name, block.get("input", {}))
                                yield f"data: {json.dumps({'type': 'tool_call', 'tool': tool_name, 'summary': summary, 'id': block.get('id', '')})}\n\n"

                    elif etype == "user":
                        msg = event.get("message", {})
                        for block in msg.get("content", []):
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_result":
                                yield f"data: {json.dumps({'type': 'tool_result', 'id': block.get('tool_use_id', ''), 'is_error': block.get('is_error', False)})}\n\n"

                    elif etype == "result":
                        errors = event.get("errors", [])
                        if event.get("is_error") and any("No conversation found" in e for e in errors):
                            # Session expired — reset and retry once with a fresh session
                            session_id = str(uuid.uuid4())
                            session_used = False
                            save_session(session_id, False)
                            session_expired = True
                        else:
                            done_data = {
                                'type': 'done',
                                'duration': event.get('duration_ms'),
                            }
                            session_used = True
                            save_session(session_id, True)
                            yield f"data: {json.dumps(done_data)}\n\n"

                stdout_thread.join(timeout=5)
                proc.wait()
                stderr_thread.join(timeout=2)

                # Check stderr for overage / rate-limit errors
                stderr_text = ' '.join(stderr_lines).lower()
                if any(kw in stderr_text for kw in OVERAGE_KEYWORDS):
                    raw = ' '.join(stderr_lines)[:300]
                    yield f"data: {json.dumps({'type': 'account_error', 'message': raw})}\n\n"

            except GeneratorExit:
                proc.kill()
                proc.wait()
                return
            finally:
                with proc_lock:
                    if current_proc is proc:
                        current_proc = None

            if not session_expired:
                break  # Normal exit — don't retry

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route("/api/usage")
@login_required
def api_usage():
    """Return token usage percentages. Always fetches fresh data."""
    try:
        subprocess.run(
            ["python3", USAGE_SCRIPT],
            timeout=30, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    try:
        with open(USAGE_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"error": "no data"}), 503


PIPER_MODEL = os.path.expanduser("~/piper-voices/en_US-ryan-high.onnx")
PIPER_PYTHON = os.path.expanduser("~/claude-chat/venv/bin/python3")

@app.route("/tts", methods=["POST"])
@login_required
def tts():
    """Convert text to speech using Piper and return a WAV audio stream."""
    data = request.get_json()
    text = (data or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400

    try:
        proc = subprocess.run(
            [PIPER_PYTHON, "-m", "piper", "--model", PIPER_MODEL, "--output-raw"],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=30,
            cwd=os.path.expanduser("~"),
        )
        if proc.returncode != 0:
            return jsonify({"error": "piper failed", "detail": proc.stderr.decode()}), 500

        # Piper --output-raw produces raw 16-bit mono PCM at 22050 Hz.
        # Wrap it in a WAV header so browsers can play it directly.
        pcm = proc.stdout
        sample_rate = 22050
        num_channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm)
        chunk_size = 36 + data_size

        import struct
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", chunk_size, b"WAVE",
            b"fmt ", 16, 1, num_channels, sample_rate,
            byte_rate, block_align, bits_per_sample,
            b"data", data_size,
        )
        wav = io.BytesIO(header + pcm)
        return send_file(wav, mimetype="audio/wav", as_attachment=False)

    except subprocess.TimeoutExpired:
        return jsonify({"error": "piper timed out"}), 504


@app.route("/interrupt", methods=["POST"])
@login_required
def interrupt():
    """Kill the current response so the frontend can send a new message."""
    kill_current()
    return jsonify({"status": "ok"})


@app.route("/new", methods=["POST"])
@login_required
def new_chat():
    global session_id, session_used
    kill_current()
    session_id = str(uuid.uuid4())
    session_used = False
    save_session(session_id, False)
    return jsonify({"session_id": session_id})


if __name__ == "__main__":
    print(f"Session ID: {session_id} (resumed: {session_used})")
    app.run(host="0.0.0.0", port=5000, threaded=True)
