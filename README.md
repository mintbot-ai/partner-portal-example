# Brand Partner reference portal

A minimal, runnable example of a [**Mintbot Brand Partner**](https://mintbot.how/partner-api/)
storefront. Fork it, rebrand it, deploy it.

This portal pretends to be **AcmeAI**, a fictitious reseller of AI
assistants. End users visit `acmeai.example.com`, pick a plan, are sent
to a Stripe checkout session billed by **Digital Cash OÜ** (the legal
payee on the mintbot side), and are returned to AcmeAI's thank-you page
after payment. They never see the string `mintbot` anywhere.

The portal also exposes a webhook receiver that:

- verifies the `X-Mintbot-Signature` HMAC,
- stores every received event in SQLite,
- renders an `/admin` view (HTTP Basic Auth) so you can eyeball the
  end-to-end loop.

Use this to test the MintOffice integration itself without writing curl
by hand, or as a starting point for your own portal.

---

## What's covered

- `GET  /` — landing page (plan cards, prices, featured plan ribbon)
- `GET  /buy` — plan picker form (`?plan=<slug>` pre-selects). Fetches
  the partner's `allowed_credit_options` from MintOffice and offers the
  customer a choice between bundling an LLM credit pack (e.g. $10 / $20
  / $50) or buying **VPS only** and bringing their own Codex / Claude
  API key after the panel opens.
- `POST /buy` — calls MintOffice `POST /api/v1/orders`, redirects to Stripe
- `GET  /thank-you` — post-payment landing
- `GET  /cancel` — abandoned-checkout landing
- `POST /webhooks/mintoffice` — signature-verified inbound event ingest
- `GET  /admin` — Basic Auth event browser (paginated, filter by event type)
- `GET  /healthz` — liveness probe (includes a SQLite read+write check)

Sane defaults out of the box:

- **Modern dark UI** — Inter + JetBrains Mono, gradient hero, card-based
  plan picker, color-coded event-type badges on `/admin`. Fork the CSS in
  `app/templates/base.html` for a colour rebrand without touching markup.
- **Defence-in-depth security headers** — CSP, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`,
  and `Strict-Transport-Security` (when served over HTTPS).
- **Retries on transient MintOffice failures** — idempotent POSTs to
  `/orders` retry on 502/503/504 + transport errors with capped backoff.
  Configurable via `MINTOFFICE_RETRIES` and `MINTOFFICE_TIMEOUT_SECONDS`.
- **Branded HTML 404 / 500 pages** — same look as the rest of the portal;
  JSON returned for `Accept: application/json` clients.
- **Test-mode banner** when pointed at the dev MintOffice.

## What's NOT covered (intentionally)

- A real product catalogue, real prices, real branding
- Database migrations beyond a single `CREATE TABLE`
- HA, retries, queue backpressure — single-process and SQLite
- Multi-tenant operation — one `.env` per deployment

## Requirements

- Python 3.13+ (or just Docker)
- A MintOffice **Partner API key** — generate yours at
  [`mint.mintbot.ai/dashboard#api-access`](https://mint.mintbot.ai/dashboard#api-access)
- The **webhook signing secret** for that partner (optional — only needed
  once you set a Webhook URL on your partner row). The dashboard generates
  it automatically; copy it to `.env` then. Until you do, the receiver
  path will 401 every event, but the rest of the portal works.

## Run locally

```bash
cp .env.example .env       # edit values
pip install -e .
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/>.

> The Stripe `success_url` / `cancel_url` MintOffice receives must be
> HTTPS — the API rejects `http://`. For purely local UI work this still
> works (everything up to the redirect), but the actual checkout flow
> needs a public HTTPS URL: Cloudflare Tunnel, Tailscale Funnel, ngrok,
> or a small public VPS.

## Run via Docker

```bash
cp .env.example .env       # edit values
docker compose up --build
```

The SQLite database lives in a named volume so events survive restarts.

## Configure your MintOffice partner row

In the [MintOffice dashboard](https://mint.mintbot.ai/dashboard) set:

- **Webhook URL** → `https://<your-portal-host>/webhooks/mintoffice`
  (must be reachable from the public internet — `127.0.0.1` and private
  IPs are rejected by the MintOffice SSRF guard)

The webhook secret is generated automatically and shown once at creation.
Paste it into `MINTOFFICE_WEBHOOK_SECRET` in `.env`. Same for the API key
into `MINTOFFICE_API_KEY`.

## Webhook signature contract

Every inbound `POST` to `/webhooks/mintoffice` carries:

```
Content-Type: application/json
X-Mintbot-Signature: t=<unix_ts>,v1=<hex>
X-Mintbot-Event-Id: evt_<order>_<type>_<ms>
X-Mintbot-Event-Type: order.created | order.paid | agent.ready | …
```

The signed payload is `f"{ts}.{raw_body}"` HMAC-SHA256-ed with the
partner's webhook secret. Reference verifier in `app/webhooks.py`.

A signature is rejected if:

- the header doesn't parse as `t=<int>,v1=<hex>`,
- `|now − t| > 300 s` (replay protection),
- the HMAC compare fails (constant-time).

## Events you'll receive

Each event arrives as a `POST` with a JSON body. The handful you care
about for a basic flow:

- **`order.created`** — confirmed Stripe Checkout Session was minted.
  Payload: `{order_id, tier, duration_months, checkout_url, external_id}`.
- **`order.paid`** — customer paid. Revenue split is finalised. Payload:
  `{order_id, gross_cents, partner_cut_cents, currency, paid_at}`.
- **`agent.ready`** — agent VPS is up and the panel is reachable. Payload:
  `{order_id, agent_id, panel_url, expires_at}`. Email this to the customer.
- **`agent.failed`** — deploy didn't complete. Payload: `{order_id,
  agent_id, step, reason}`. Refund flow / manual triage on your side.

Full list and JSON schemas at <https://mintbot.how/partner-api/#webhooks>.

## Layout

```
partner-portal-example/
├── app/
│   ├── main.py          FastAPI routes
│   ├── config.py        env loading
│   ├── mintoffice.py    MintOffice API client (httpx)
│   ├── webhooks.py      signature verify
│   ├── db.py            SQLite helpers
│   └── templates/       Jinja2 templates (AcmeAI branding)
├── scripts/
│   └── send_test_webhook.py   craft a signed event from the CLI
├── tests/               pytest — webhook + buy flow with mocked httpx
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Testing the webhook receiver locally

Crafting an HMAC-signed body by hand is annoying — use the bundled helper
instead:

```bash
# Reads MINTOFFICE_WEBHOOK_SECRET from .env, posts a synthetic order.paid
# event to the local receiver, prints the response.
python scripts/send_test_webhook.py

# Replay the same event id to verify your idempotency handling.
python scripts/send_test_webhook.py --id evt_replay_me

# Force the stale-timestamp branch (>5 minute skew → must be rejected).
python scripts/send_test_webhook.py --skew 1000
```

`python scripts/send_test_webhook.py --help` lists every flag (custom URL,
event type, JSON payload, etc.). Useful for smoke-testing the receiver
during rebranding without round-tripping through Stripe / MintOffice.

## Test-mode banner

When `MINTOFFICE_API_URL` points at `mint.mintbot.dev` (the default), every
page renders a yellow `TEST MODE` strip at the top so customers can't be
shown a perfectly normal-looking checkout that quietly bills via the dev
environment. Switching to `https://mint.mintbot.ai` makes the banner
disappear — production traffic looks clean.

## Branding

The default templates pretend to be **AcmeAI**. Change:

- `PARTNER_BRAND` in `.env` — drives the Stripe line-item name, page
  titles, and the header/footer text. Set it once and most of the visible
  copy follows.
- `app/templates/base.html` — the `<style>` block at the top has CSS
  variables (`--accent`, `--accent-2`, `--bg`, `--bg-elev`, `--radius`).
  Tweak those for a colour rebrand without touching markup. The two
  accent colours flow into the brand-mark dot, button gradients, focus
  rings, and the featured-plan ribbon.
- `app/main.py` — `PLANS` dict has the plan slugs, prices (in cents),
  blurbs, and a `featured` flag that adds the "Recommended" ribbon.
- The rest of `app/templates/*.html` — copy lives here. Estonian /
  Spanish / etc. translations: fork these files.

### Branding the agent itself (panel + persona)

The storefront in **this** repo handles the *purchase* — landing page,
plan picker, Stripe checkout, webhook receipts, admin console. The
*agent* the customer gets (the chat panel + the assistant's voice and
identity) is a separate skin shipped from a different repo:

> **[`mintbot-ai/agent-template`](https://github.com/mintbot-ai/agent-template)** — fork this to skin the agent's panel and rewrite its persona.

That repo defines three files mintbot pulls at agent deploy time:

| File | Purpose |
|------|---------|
| `theme/theme.css` + `theme/theme.json` (+ optional `theme.js`) | Panel look & feel — colours, type, radius, layout tweaks. |
| `persona/system_prompt.md.j2` | **Full persona override** — Jinja2 template that REPLACES mintbot's bundled assistant persona. The starter file is a fully-worked AcmeAI persona you can adapt; on a real white-label deploy the agent never says "mintbot" in chat, only your brand name. |
| `persona/brand_layer.md`      | Short voice & tone overlay — *appended* on top of whichever persona is active. Use this if `system_prompt.md.j2` is too heavy and you just want a few voice notes. |

End-to-end picture:

```
┌──────────────────────────┐       ┌──────────────────────────┐
│ THIS REPO                │       │ mintbot-ai/agent-template│
│ partner-portal-example   │       │ (skin + persona fork)    │
│                          │       │                          │
│ • landing + plan picker  │       │ • theme/theme.css        │
│ • Stripe checkout        │       │ • theme/theme.json       │
│ • webhook receiver       │       │ • persona/*.md(.j2)      │
└──────────────┬───────────┘       └─────────────┬────────────┘
               │ POST /api/v1/orders             │
               │                                 │ git clone (deploy)
               ▼                                 ▼
        ┌──────────────────────────────────────────────┐
        │ MintOffice  (mint.mintbot.ai / .dev)         │
        │ — provisions the agent VPS                   │
        │ — splices your theme into the panel          │
        │ — renders your persona into the SOUL.md      │
        └──────────────────────────────────────────────┘
                            │
                            ▼
                ┌──────────────────────┐
                │ agentNNN.mintbot.ai  │  ← the customer's running agent
                │ (panel + chat)       │     wearing your brand
                └──────────────────────┘
```

You can ship this storefront WITHOUT a custom `agent-template` fork —
the customer just gets the default mintbot-styled panel and persona.
For a true white-label experience where the agent itself wears your
brand, fork `mintbot-ai/agent-template`, point your partner row at it
in MintOffice (`Settings → Template` field), and the next deploy picks
it up. See that repo's `docs/customizing.md` and `docs/publishing.md`
for the full walkthrough.

## Troubleshooting

- **MintOffice returned 401** — wrong API key, or your partner row is
  flagged `disabled`. Check `mint.mintbot.ai/dashboard#api-access`.
- **MintOffice returned 422** on `/buy` — most likely `success_url` /
  `cancel_url` is `http://` (must be HTTPS) or a path-only string.
- **Webhook events never arrive** — confirm `Webhook URL` is set on
  your partner row, and that the public URL actually resolves from the
  open internet (curl from a different network). MintOffice retries up
  to 7 times with backoff before giving up.
- **`/admin` 401** — `ADMIN_PASSWORD` is empty or doesn't match.

## License

MIT.
