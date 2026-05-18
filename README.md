# partner-portal-example

A minimal, runnable reference implementation of a **Brand Partner** that
sits in front of [MintOffice](https://mint.mintbot.ai/) — the white-label
side of [mintbot.ai](https://mintbot.ai/).

This portal pretends to be **AcmeAI**, a fictitious reseller. End users
visit `acmeai.example.com`, pick a plan, are sent to a Stripe checkout
session billed by **Digital Cash OÜ** (the legal payee on the mintbot
side), and are returned to AcmeAI's thank-you page after payment. They
never see the string "mintbot" anywhere.

The portal also exposes a webhook receiver that:

- verifies the `X-Mintbot-Signature` HMAC,
- stores every received event in SQLite,
- renders an `/admin` view (HTTP Basic Auth) so you can eyeball the
  end-to-end loop.

Use this to test MintOffice itself without writing curl by hand.

---

## What's covered

- `POST /buy` → MintOffice `POST /api/v1/orders` → Stripe redirect
- `GET /thank-you` and `GET /cancel` — post-checkout landing pages
- `POST /webhooks/mintoffice` — signature verify + storage
- `GET /admin` — Basic Auth event browser

## What's NOT covered (intentionally)

- A real product catalogue, real prices, real branding
- Database migrations beyond a single CREATE TABLE
- HA, retries, queue backpressure — single-process and SQLite
- Multi-tenant operation — one `.env` per deployment

## Requirements

- Python 3.13+
- A MintOffice **Partner API key** (issued by the mintbot team from the
  CatOffice admin → Partners tab; lives on `mintoffice_partners.api_key`)
- The **webhook secret** for that partner (issued at the same time;
  lives on `mintoffice_partners.webhook_secret`)

## Run locally

```bash
cp .env.example .env       # edit values
pip install -e .
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/>.

## Run via Docker

```bash
cp .env.example .env       # edit values
docker compose up --build
```

Open <http://127.0.0.1:8000/>.

The SQLite database lives in a named volume so events survive restarts.

## Configure your MintOffice partner row

In the **MintOffice dashboard** (`https://mint.mintbot.dev/dashboard` for
DEV), set:

- **Webhook URL** → `https://<your-portal-host>/webhooks/mintoffice`
  (must be reachable from the public internet — Cloudflare Tunnel, a
  small VPS, etc. — `127.0.0.1` is rejected by the MintOffice SSRF guard)

The webhook secret is generated automatically and shown once. Paste it
into `MINTOFFICE_WEBHOOK_SECRET` in `.env`. Likewise the API key into
`MINTOFFICE_API_KEY`.

## Webhook signature contract

`POST` to your `/webhooks/mintoffice` endpoint carries:

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

## Layout

```
partner-portal-example/
├── app/
│   ├── main.py          FastAPI routes
│   ├── config.py        env loading
│   ├── mintoffice.py    MintOffice API client (httpx)
│   ├── webhooks.py      signature verify + storage
│   ├── db.py            SQLite helpers
│   └── templates/       Jinja2 templates (AcmeAI branding)
├── tests/               pytest — webhook + buy flow with mocked httpx
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Branding

The default templates pretend to be **AcmeAI**. Change:

- `PARTNER_BRAND` in `.env` (drives Stripe line-item name + page titles)
- `app/templates/base.html` for the visible chrome
- the placeholder colours in `app/templates/base.html`'s inline CSS

## License

MIT.
