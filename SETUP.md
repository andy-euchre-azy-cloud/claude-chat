# Claude Chat — Setup Guide

A browser-based chat UI that wraps your local Claude Code CLI.
Responses stream in real time. Supports voice input, TTS, mid-stream
interrupt, and usage stats.

---

## Prerequisites

- **Python 3.10+**
- **Claude Code CLI** installed and authenticated on this machine
  (`claude --version` should work; `claude -p "hi"` should return a response)

That's it. The app talks to *your* Claude via the local `claude` binary —
no API keys or extra config needed.

---

## Installation

```bash
git clone <repo-url> claude-chat
cd claude-chat

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Running

```bash
source venv/bin/activate
python app.py
```

Then open `http://localhost:5000` in your browser.

To expose it publicly, run a tunnel of your choice pointed at port 5000
(e.g. Cloudflare Tunnel, ngrok, etc.).

---

## Running as a systemd user service (optional)

Create `~/.config/systemd/user/claude-chat.service`:

```ini
[Unit]
Description=Claude Chat Web App
After=network.target

[Service]
WorkingDirectory=/path/to/claude-chat
ExecStart=/path/to/claude-chat/venv/bin/python app.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-chat.service
```

---

## Usage stats widget

The usage bar in the top-right reads rate-limit headers from the
Anthropic API using your Claude Code OAuth token
(`~/.claude/.credentials.json`). It works automatically as long as
Claude Code is installed and authenticated — no extra setup needed.
