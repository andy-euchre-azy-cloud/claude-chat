#!/usr/bin/env python3
"""
Fetches real usage data from Anthropic API rate-limit headers.
Makes two minimal API calls:
  1. Haiku — reads unified 5h + 7d headers
  2. Sonnet — reads seven_day_sonnet header (only returned for Sonnet calls)
Writes results to ~/claude-chat/.usage_stats.json.

Automatically refreshes the OAuth token if it is expired or close to expiry.
"""

import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

CREDS_FILE  = os.path.expanduser('~/.claude/.credentials.json')
OUT_FILE    = os.path.expanduser('~/claude-chat/.usage_stats.json')
TOKEN_URL   = 'https://platform.claude.com/v1/oauth/token'
CLIENT_ID   = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
REFRESH_BUFFER = 300  # refresh if token expires within 5 minutes


def _load_creds():
    with open(CREDS_FILE) as f:
        return json.load(f)


def _save_creds(creds):
    with open(CREDS_FILE, 'w') as f:
        json.dump(creds, f, indent=2)


def _refresh_token(refresh_token):
    """Exchange a refresh token for a new access token."""
    body = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read().decode())


def get_api_key():
    """Return a valid OAuth access token, refreshing it first if needed."""
    creds = _load_creds()
    oauth = creds['claudeAiOauth']

    expires_at_ms = oauth.get('expiresAt', 0)
    expires_at_s  = expires_at_ms / 1000 if expires_at_ms > 1e10 else expires_at_ms
    needs_refresh = expires_at_s - time.time() < REFRESH_BUFFER

    if needs_refresh and oauth.get('refreshToken'):
        data = _refresh_token(oauth['refreshToken'])
        oauth['accessToken']  = data['access_token']
        if 'refresh_token' in data:
            oauth['refreshToken'] = data['refresh_token']
        if 'expires_in' in data:
            oauth['expiresAt'] = int((time.time() + data['expires_in']) * 1000)
        creds['claudeAiOauth'] = oauth
        _save_creds(creds)

    return oauth['accessToken']


def _probe(model, token):
    """Make a minimal API call and return response headers."""
    body = json.dumps({
        'model': model,
        'max_tokens': 1,
        'messages': [{'role': 'user', 'content': 'hi'}],
    }).encode()

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'x-api-key': token,
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.headers
    except urllib.error.HTTPError as e:
        return e.headers


def _parse_bucket(headers, key):
    """Extract utilization/reset/status for a named bucket key (e.g. '5h', '7d')."""
    util   = headers.get(f'anthropic-ratelimit-unified-{key}-utilization')
    reset  = headers.get(f'anthropic-ratelimit-unified-{key}-reset')
    status = headers.get(f'anthropic-ratelimit-unified-{key}-status')
    if util is None:
        return None
    reset_ts = int(reset) if reset else None
    reset_iso = (
        datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
        if reset_ts else None
    )
    return {
        'utilization': float(util),
        'pct': round(float(util) * 100, 1),
        'reset_ts': reset_ts,
        'reset_iso': reset_iso,
        'status': status or 'unknown',
    }


def _find_sonnet_bucket(headers):
    """
    Scan all response headers for the sonnet-specific 7d utilization.
    The exact key name is model-plan-dependent; try known patterns and
    fall back to scanning for any header containing 'sonnet' and 'utilization'.
    """
    candidates = [
        'seven_day_sonnet',
        '7d_sonnet',
        'seven-day-sonnet',
        '7d-sonnet',
    ]
    for key in candidates:
        bucket = _parse_bucket(headers, key)
        if bucket is not None:
            return bucket

    # Fallback: scan all headers
    for name in headers.keys():
        name_lower = name.lower()
        if 'sonnet' in name_lower and 'utilization' in name_lower:
            util = headers.get(name)
            try:
                return {
                    'utilization': float(util),
                    'pct': round(float(util) * 100, 1),
                    'reset_ts': None,
                    'reset_iso': None,
                    'status': 'unknown',
                    '_header': name,
                }
            except (TypeError, ValueError):
                pass
    return None


def fetch_usage():
    """Make minimal API calls and extract rate-limit headers."""
    token = get_api_key()

    # Probe 1: Haiku — gets 5h + 7d unified buckets cheaply
    haiku_headers = _probe('claude-haiku-4-5-20251001', token)
    buckets = {}
    for prefix in ('5h', '7d'):
        b = _parse_bucket(haiku_headers, prefix)
        if b is not None:
            buckets[prefix] = b

    # Probe 2: Sonnet — gets seven_day_sonnet bucket
    sonnet_headers = _probe('claude-sonnet-4-6', token)
    sonnet_bucket = _find_sonnet_bucket(sonnet_headers)
    if sonnet_bucket is not None:
        buckets['7d_sonnet'] = sonnet_bucket

    if '5h' not in buckets and '7d' not in buckets:
        result = {'error': 'no rate-limit headers — token may be expired or API error'}
    else:
        result = {
            'updated': datetime.now(timezone.utc).isoformat(),
            'buckets': buckets,
            'pcts': {
                'session':     buckets.get('5h',       {}).get('pct', 0),
                'week':        buckets.get('7d',       {}).get('pct', 0),
                'sonnet_week': buckets.get('7d_sonnet', {}).get('pct'),
            },
            'resets': {
                'session':     buckets.get('5h',       {}).get('reset_iso'),
                'week':        buckets.get('7d',       {}).get('reset_iso'),
                'sonnet_week': buckets.get('7d_sonnet', {}).get('reset_iso'),
            },
        }

    with open(OUT_FILE, 'w') as f:
        json.dump(result, f, indent=2)

    return result


if __name__ == '__main__':
    r = fetch_usage()
    if r.get('error'):
        print(f"Error: {r['error']}")
    else:
        for label, key in [('Session (5h)', '5h'), ('Week (7d)', '7d'), ('Sonnet 7d', '7d_sonnet')]:
            b = r['buckets'].get(key, {})
            pct = b.get('pct', '?')
            status = b.get('status', '?')
            print(f"{label}: {pct}% ({status})")
        if '7d_sonnet' not in r['buckets']:
            print("Sonnet 7d: not available (probe 429 — likely active session in progress)")
