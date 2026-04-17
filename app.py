#!/usr/bin/env python3
"""Web chat interface for Claude Code CLI with mid-stream messaging support."""

import json
import os
import queue
import subprocess
import threading
import time
import uuid

from flask import Flask, Response, render_template, request, jsonify

app = Flask(__name__)

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


@app.route("/")
def index():
    return render_template("index.html", model=get_current_model())


@app.route("/api/model", methods=["POST"])
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
def api_status():
    """Return current session info."""
    busy = current_proc is not None and current_proc.poll() is None
    return jsonify({
        "session_id": session_id[:8] + "…",
        "active": session_used,
        "busy": busy,
    })


@app.route("/chat", methods=["POST"])
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
                cwd="/home/andy",
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


@app.route("/interrupt", methods=["POST"])
def interrupt():
    """Kill the current response so the frontend can send a new message."""
    kill_current()
    return jsonify({"status": "ok"})


@app.route("/new", methods=["POST"])
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
