"""Send a synthetic, properly-signed webhook to a running portal.

Saves you from crafting the HMAC header by hand while you verify your
receiver, your idempotency handling, or your /admin browser.

Usage
-----

    python scripts/send_test_webhook.py [--type EVENT_TYPE] [--url URL]
                                        [--secret SECRET] [--id EVENT_ID]
                                        [--payload JSON]

Defaults read from .env in the current directory if present (looks for
MINTOFFICE_WEBHOOK_SECRET); falls back to the values the user passes.

Examples
--------

    # Simple — uses .env, posts a fake order.paid to localhost:8000.
    python scripts/send_test_webhook.py

    # Replay the same event to test idempotency (expect "duplicate" log).
    python scripts/send_test_webhook.py --id evt_test_fixed

    # Different portal, different event type, custom payload.
    python scripts/send_test_webhook.py \\
        --url https://acmeai.example.com/webhooks/mintoffice \\
        --type agent.ready \\
        --payload '{"order_id": 42, "agent_id": 7, "panel_url": "https://a7.example/"}'
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_dotenv_secret(env_path: Path) -> str | None:
    """Tiny stdlib .env reader — just enough to find the webhook secret."""
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "MINTOFFICE_WEBHOOK_SECRET":
            return val.strip().strip('"').strip("'")
    return None


def make_signature(secret: str, ts: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={digest}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/mintoffice",
                        help="Receiver URL (default: %(default)s)")
    parser.add_argument("--secret",
                        help="Webhook signing secret. Defaults to "
                             "MINTOFFICE_WEBHOOK_SECRET from .env / env.")
    parser.add_argument("--type", dest="event_type", default="order.paid",
                        help="X-Mintbot-Event-Type header (default: %(default)s)")
    parser.add_argument("--id", dest="event_id",
                        help="X-Mintbot-Event-Id header (default: random)")
    parser.add_argument("--payload",
                        help="Raw JSON body to sign and POST. Default: a small "
                             "order.paid sample.")
    parser.add_argument("--skew", type=int, default=0,
                        help="Add this many seconds to the timestamp — use a "
                             "value > 300 to test the stale-timestamp guard.")
    args = parser.parse_args()

    secret = args.secret or os.environ.get("MINTOFFICE_WEBHOOK_SECRET") \
        or load_dotenv_secret(Path(".env"))
    if not secret:
        print("error: no webhook secret. Pass --secret, set "
              "MINTOFFICE_WEBHOOK_SECRET, or add it to .env in cwd.",
              file=sys.stderr)
        return 2

    if args.payload is not None:
        body = args.payload.encode("utf-8")
    else:
        body = json.dumps({
            "order_id": 1,
            "tier": "s2",
            "duration_days": 30,
            "gross_cents": 1500,
            "partner_cut_cents": 500,
            "currency": "usd",
            "paid_at": int(time.time()),
        }).encode("utf-8")

    ts = int(time.time()) + args.skew
    sig = make_signature(secret, ts, body)
    event_id = args.event_id or f"evt_test_{ts}"

    req = urllib.request.Request(
        args.url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Mintbot-Signature": sig,
            "X-Mintbot-Event-Id": event_id,
            "X-Mintbot-Event-Type": args.event_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"{resp.status} {resp.reason}")
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(f"{e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"network error: {e.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
