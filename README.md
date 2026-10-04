# Allied Tours & Travel Agency: Client Communication Platform

*Your one-stop travel shop.*

A Flask monolith that lets Allied staff organise clients into categories, send personalised SMS, WhatsApp and
email campaigns, and handle replies in a single shared inbox.

**Stack:** Flask, Jinja2, Tailwind CSS, vanilla JavaScript, PostgreSQL, SQLAlchemy and Alembic, Celery with Redis,
Gunicorn and Nginx.

## Core workflow

1. Add or import clients (CSV), with phone, email and per-channel consent.
2. Assign them to categories (VIP, Safari, Corporate…) and tags.
3. Create a campaign: choose an audience by category, tags, status and location, then a channel. Write the
   message using `{{first_name}}`, `{{last_name}}` and `{{company}}`.
4. Preview, then save as a draft, send now, or schedule (Africa/Nairobi by default).
5. Celery delivers the messages in the background. Provider webhooks update the status
   (sent, delivered, read or failed).
6. Replies arrive in the **Inbox**, attached to the client's profile and to the campaign that prompted them.
   Staff can reply, assign the conversation, add internal notes, and resolve it.

## Roles

| Role | Can do |
|------|--------|
| Super Admin | Everything, including managing users |
| Administrator | Everything except managing users |
| Marketing Manager | Clients (view/categorise/export), categories, campaigns, templates, analytics |
| Customer Support Agent | View clients, reply in conversations, assign them |
| Viewer | Read-only |

Permissions are enforced in the backend with `@permission_required`, not only by hiding buttons.

## Local development

Requirements: Python 3.12, Node 20 (for building the Tailwind CSS), and PostgreSQL. Redis is optional.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
npm ci && npm run build:css                   # or: npm run watch:css while editing templates

cp .env.example .env                          # then edit DATABASE_URL, SECRET_KEY, ...
flask --app run db upgrade                    # create the schema
flask --app run seed                          # roles, default categories/tags/templates, first super admin
flask --app run seed-demo --count 120         # optional demo clients
flask --app run run                           # http://localhost:5000
```

With `TASK_BACKEND=thread` (the default in `.env.example`), campaigns are sent from a background thread, so
Redis isn't needed. Scheduled campaigns need a scheduler process: run `flask --app run run-scheduler`, or use
Celery as described below.

To use Celery locally:

```bash
# .env: TASK_BACKEND=celery, REDIS_URL=redis://localhost:6379/0
celery -A celery_worker.celery worker --loglevel=info        # add --pool=solo on Windows
celery -A celery_worker.celery beat --loglevel=info          # queues scheduled campaigns every minute
```

Messaging providers default to `console`, which logs messages instead of sending them. Every workflow can be
tried without any provider accounts.

Other commands: `flask create-user`, `flask dispatch-due` (queues due campaigns once), and
`flask db migrate -m "..."` (after changing models).

## Tests

```bash
pytest -q                                                    # in-memory SQLite
TEST_DATABASE_URL=postgresql://user:pass@localhost/allied_test pytest -q   # against PostgreSQL
```

The suite covers authentication and roles, clients, CSV import, campaigns (audience, scheduling,
double-send guard, opt-outs), the messaging service and providers, webhooks (signatures, idempotency,
out-of-order statuses), conversations, and analytics. CI (`.github/workflows/ci.yml`) runs the suite on
PostgreSQL and SQLite, checks that the migrations match the models, and builds the Docker images.

## Production deployment (Docker Compose)

The stack:

| Service | Role |
|---------|------|
| `nginx` | TLS termination, HTTP→HTTPS redirect, serves `/static`, proxies to Gunicorn |
| `web` | Flask app under Gunicorn (`gunicorn.conf.py`, `wsgi.py`) |
| `worker` | Celery worker: campaign delivery, retries, individual replies |
| `beat` | Celery beat: dispatches scheduled campaigns every 60 seconds |
| `migrate` | Runs `flask db upgrade` once before the web, worker and beat services start |
| `db` | PostgreSQL 16 (volume `pgdata`) |
| `redis` | Celery broker and rate-limit storage (volume `redisdata`) |

Uploaded attachments and CSV imports live in the shared `uploads` volume, so the worker can read them.

```bash
cp .env.example .env
#  Set at least: SECRET_KEY, POSTGRES_PASSWORD, APP_BASE_URL=https://your-domain,
#  ADMIN_EMAIL/ADMIN_PASSWORD, and the provider credentials described below.

# TLS certificate: deploy/nginx/certs/fullchain.pem and privkey.pem
# (Let's Encrypt; the HTTP server already serves /.well-known/acme-challenge/ from the certbot-webroot volume).
# For a quick trial, use a self-signed certificate:
mkdir -p deploy/nginx/certs
openssl req -x509 -nodes -newkey rsa:2048 -days 30 -subj "/CN=localhost" \
  -keyout deploy/nginx/certs/privkey.pem -out deploy/nginx/certs/fullchain.pem

docker compose up -d --build
docker compose exec web flask seed
docker compose ps                      # web should be "healthy" (GET /healthz checks the database)
docker compose logs -f web worker
```

Compose forces `APP_ENV=production`, `TASK_BACKEND=celery`, `SESSION_COOKIE_SECURE=true` and
`TRUSTED_PROXY_COUNT=1`, so client IPs come from Nginx for rate limiting and audit logs. The app refuses to
start in production without a real `SECRET_KEY`.

**Upgrades:** `git pull && docker compose up -d --build`. The `migrate` service applies new migrations first.

**Backups:** `scripts/backup.sh` writes a compressed `pg_dump` to `backups/` and keeps 14 days of dumps.
Schedule it with cron; restore instructions are in the script header.

**Monitoring:** `GET /healthz` returns 200 when the database is reachable and 503 otherwise. Logs go to stdout
(`docker compose logs`). The **Settings → Channels** page shows provider configuration and recent webhook events.

## Messaging providers and webhooks

All credentials come from environment variables, and none are sent to the browser. Every provider sits behind
the same interface (`app/messaging/`), and all sending goes through `MessageService`, so swapping a provider
doesn't affect campaigns or the inbox.

| Channel | Provider (`*_PROVIDER`) | Webhook URL to register | Verification |
|---------|------------------------|-------------------------|--------------|
| SMS | `africastalking` | `https://your-domain/webhooks/sms?token=<SMS_WEBHOOK_TOKEN>` (incoming messages **and** delivery reports) | shared token, constant-time compare |
| WhatsApp | `meta` (official WhatsApp Business Cloud API) | `https://your-domain/webhooks/whatsapp`, verify token = `WHATSAPP_VERIFY_TOKEN`; subscribe to `messages` | `X-Hub-Signature-256` HMAC using `WHATSAPP_APP_SECRET` |
| Email | `smtp` | `https://your-domain/webhooks/email` (from your inbound relay or ESP event forwarder) | `X-Allied-Signature` = hex HMAC-SHA256 of the raw body using `EMAIL_WEBHOOK_SECRET` |

Only the official Meta WhatsApp Business API is supported. There is no WhatsApp Web automation or scraping.
WhatsApp only allows a business to start a conversation with a Meta-approved template. Set the template's
**Provider template name** on the message template in the app. Free-form replies are allowed within 24 hours of
the client's last message.

Email webhook body (a single event, or `{"events": [...]}`):

```json
{"type": "delivered", "message_id": "<id@domain>"}
{"type": "bounced", "message_id": "<id@domain>", "reason": "Mailbox unavailable"}
{"type": "inbound", "id": "abc123", "from": "Jane <jane@example.com>", "to": "info@alliedtours.co.ke",
 "subject": "Re: Safari offer", "text": "Interested!"}
```

Email opens are also tracked with a pixel, and every campaign email includes a signed one-click unsubscribe link.

Webhooks are idempotent. Each event is stored once, keyed by the provider's event id, and status updates only
move a message forward (a late "sent" never overwrites "read").

## Opt-outs and consent

Each client has a separate consent status per channel. Clients are skipped, and counted as excluded in the
campaign wizard, when they have opted out or have no address for the chosen channel. An SMS or WhatsApp reply of
STOP/UNSUBSCRIBE, or the email unsubscribe link, opts the client out of that channel. Consent is checked again
for each message at send time, so an opt-out after scheduling is respected. Every opt-out is written to the audit
log.

## Security summary

- CSRF on every form and fetch request, except webhooks, which use signatures instead.
- Secure, HttpOnly, SameSite cookies, and scrypt password hashing.
- Single-use, time-limited password reset links.
- RBAC on every route, rate limiting on login, password reset and webhooks, and SQLAlchemy ORM everywhere.
- Strict CSP (`script-src 'self'`, no inline scripts), plus X-Frame-Options, nosniff, Referrer-Policy and HSTS.
- Template personalisation uses plain substitution (no Jinja), with values HTML-escaped in emails.
- CSV export neutralises spreadsheet formulas.
- Audit log of logins, client changes, imports, campaign actions, opt-outs and user management.

## Project layout

```
app/
  auth/ dashboard/ clients/ categories/ campaigns/ conversations/ settings/ webhooks/ public/   # blueprints
  messaging/      # provider interfaces (sms.py, whatsapp.py, email.py) and MessageService
  models/         # SQLAlchemy models
  templates/      # Jinja templates; reusable components in templates/components/
  static/         # Tailwind input.css -> app.css, vanilla JS modules
  tasks.py        # background jobs (Celery / thread / sync)
migrations/       # Alembic migrations
deploy/nginx/     # Nginx config (certificates go in deploy/nginx/certs, not committed)
scripts/backup.sh
```
