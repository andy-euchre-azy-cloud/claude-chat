# Claude Chat — Setup Guide

> This document is written for Isaac's Claude Code instance on nerdbox.
> Isaac: just hand this file to your Claude and it can handle the setup.

---

## What This Is

A browser-based chat UI that wraps your local `claude` CLI. You type in
a browser, responses stream in real time, with voice input, TTS,
mid-stream interrupt, and a usage stats widget. It talks to *your*
Claude via your local `claude` binary — no extra API keys needed.

---

## What Claude Needs to Do

### 1. Verify prerequisites

```bash
python3 --version        # needs 3.10+
claude --version         # Claude Code must be installed
claude -p "hi"           # must return a response (confirms auth)
```

If `claude` isn't installed or authenticated, stop here and sort that
out first.

### 2. Clone the repo and install dependencies

```bash
git clone <repo-url> ~/claude-chat
cd ~/claude-chat
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run the app

```bash
source venv/bin/activate
python app.py
```

Then open `http://localhost:5000`. Verify you get a response when you
send a message.

### 4. (Optional) Set up a systemd user service for persistence

Create `~/.config/systemd/user/claude-chat.service`, substituting the
correct absolute path for the clone location:

```ini
[Unit]
Description=Claude Chat Web App
After=network.target

[Service]
WorkingDirectory=/home/isaac/claude-chat
ExecStart=/home/isaac/claude-chat/venv/bin/python app.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-chat.service
```

### 5. (Optional) Expose it publicly

Point a Cloudflare Tunnel, ngrok, or similar at `localhost:5000`.

---

## Notes for Claude

- The app spawns a subprocess per message using `claude -p --resume`.
  It will inherit whatever model and permissions the local `claude` is
  configured with.
- Usage stats are read from `~/.claude/.credentials.json` (Claude
  Code's OAuth token). This is automatic — no manual config needed.
- The session file lives at `~/claude-chat/.session_id` and persists
  across restarts. Delete it to start a fresh conversation.
- If something breaks, check `journalctl --user -u claude-chat.service`
  or just run `python app.py` in the foreground to see output directly.
