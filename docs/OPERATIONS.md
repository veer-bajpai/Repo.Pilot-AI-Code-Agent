# RepoPilot Operations Guide

## Local development

### Guest/demo profile

This is the fastest local profile. It preserves the original anonymous-cookie behavior and does not require PostgreSQL or Redis.

```powershell
Copy-Item .env.example .env
# Put GEMINI_API_KEY in .env if live Gemini runs are needed.
$env:PORT="8001"
python run.py
```

Open `http://localhost:8001`.

The three built-in demos work without Gemini. A live run requires a working Gemini key and available model quota.

### Authenticated local profile

Use this profile to exercise signup, cookies, usage, and ownership locally without a PostgreSQL server:

```powershell
$env:PORT="8002"
$env:AUTH_REQUIRED="true"
$env:AUTH_AUTO_CREATE="true"
$env:DATABASE_URL="sqlite:///C:/absolute/path/to/data/local-auth.db"
$env:JWT_SECRET="replace-this-for-local-testing"
python run.py
```

Open `http://localhost:8002`. The local auth profile returns verification/reset tokens in development when SMTP is not configured.

### PostgreSQL and Redis profile

When Docker Desktop is running:

```powershell
docker compose up --build
```

The Compose stack contains the web service, PostgreSQL, and Redis. The container entrypoint runs Alembic migrations before starting FastAPI.

For queued runs, start a worker in another shell when running outside Compose:

```powershell
celery -A backend.app.queue.celery_app worker --loglevel=INFO
```

## Environment setup

Copy `.env.example` to `.env` and configure only what the selected profile needs.

### Required production values

| Variable             | Purpose                                      |
| -------------------- | -------------------------------------------- |
| `GEMINI_API_KEY`     | Server-side Gemini credential                |
| `DATABASE_URL`       | PostgreSQL connection string                 |
| `AUTH_REQUIRED=true` | Enables authenticated SaaS mode              |
| `JWT_SECRET`         | Signs access tokens; use a long random value |
| `APP_BASE_URL`       | Public HTTPS URL and OAuth callback base     |
| `REDIS_URL`          | Celery broker/result backend                 |

### Auth and email

| Variable                                                           | Purpose                           |
| ------------------------------------------------------------------ | --------------------------------- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`                         | Google OAuth                      |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`                         | GitHub OAuth                      |
| `EMAIL_FROM`                                                       | Sender address                    |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Verification/reset email delivery |

### Billing and administration

| Variable                | Purpose                                           |
| ----------------------- | ------------------------------------------------- |
| `STRIPE_SECRET_KEY`     | Stripe server API key                             |
| `STRIPE_WEBHOOK_SECRET` | Signature verification for `/api/billing/webhook` |
| `STRIPE_PRICE_ID`       | Paid subscription price reference                 |
| `ADMIN_EMAILS`          | Comma-separated audit administrators              |

## Migrations

Run from the repository root:

```powershell
$env:DATABASE_URL="postgresql+psycopg://user:password@host:5432/repopilot"
alembic upgrade head
```

Rollback one revision only when the database backup and deployment rollback plan are ready:

```powershell
alembic downgrade -1
```

Never run `AUTH_AUTO_CREATE=true` in production. It is a test/local convenience only.

## OAuth callback configuration

Set these exact callback URLs in the provider consoles:

```text
https://your-domain.example/api/auth/oauth/google/callback
https://your-domain.example/api/auth/oauth/github/callback
```

Set `APP_BASE_URL=https://your-domain.example` so access and refresh cookies use the `Secure` flag.

## Stripe webhook configuration

Create a webhook endpoint:

```text
POST https://your-domain.example/api/billing/webhook
```

Subscribe to customer subscription lifecycle events. The endpoint verifies `stripe-signature`, maps active subscription events to the `paid` plan, and maps deletion to `free`.

## Health checks

```powershell
Invoke-WebRequest http://localhost:8001/api/health
Invoke-WebRequest http://localhost:8001/api/config
Invoke-WebRequest http://localhost:8001/api/ai/status
```

Expected health fields:

- `ok: true` means the API is responding;
- `git: true` means repository operations are available;
- `ai_configured: true` means a key is loaded, not that Gemini quota is available;
- `/api/ai/status` performs a real cached Gemini check and reports authentication, model, network, or quota errors.

## Testing

```powershell
python -m pytest backend/tests -q
python -m pytest backend/tests/test_auth.py -q
Set-Location frontend-react
npm run build
```

The Windows test suite includes symlink and Git cleanup cases that may require Developer Mode or elevated privileges. Those failures do not indicate a failed production auth or agent path.

## Render deployment

1. Create a Render Blueprint from `render.yaml`.
2. Provision the managed PostgreSQL and Redis resources.
3. Set `APP_BASE_URL` to the final HTTPS web URL.
4. Enter `GEMINI_API_KEY`, OAuth, SMTP, Stripe, and the same `JWT_SECRET` on both web and worker services.
5. Deploy the web and worker services.
6. Confirm `/api/health`, `/api/ai/status`, signup/login, OAuth callbacks, and a built-in demo.
7. Configure Stripe and OAuth callback URLs only after the final Render URL is stable.

## Incident notes

- A Gemini `quota` or `busy` response is an upstream account/model limit, not a local authentication failure.
- A `401` on `/api/sessions` in SaaS mode means the browser has no valid access cookie or API key.
- A `404` on a session/run/approval that exists for another user is intentional ownership isolation.
- A queued run requires both a reachable Redis broker and a running Celery worker.
- Public mode intentionally refuses arbitrary repository test execution. Use paid-plan policy or a private controlled environment for trusted test execution.
