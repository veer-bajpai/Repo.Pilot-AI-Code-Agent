# RepoPilot

An AI developer agent you stay in control of, powered by **Google Gemini**. Point it at a public GitHub repository, describe a bug or a small change, and it reads the code, makes a plan, edits files and (where allowed) runs the tests. It **stops for your approval before every edit and every test run**, then hands you a diff you can download or turn into a pull request.

- **Two operating modes.** Local demo mode keeps anonymous-cookie compatibility; SaaS mode uses PostgreSQL-backed accounts, OAuth, JWT cookies, API keys, teams, usage quotas, and audit logs.
- **Approval remains the product boundary.** The agent loop, guardrails, diff handling, test policy, and approval gates are preserved while ownership moves from anonymous cookies to authenticated user IDs.
- **Production integrations are explicit.** PostgreSQL, Redis/Celery, SMTP, OAuth, Stripe, and Gemini are configured through environment variables and documented in [docs/OPERATIONS.md](docs/OPERATIONS.md).
- **Try it with no key at all.** Three built-in demos (each a real bug with failing tests) run a scripted fix through the same approval workflow, so the app is useful even before you add a Gemini key.

## Quick start

You need Python 3.10+ and git.

```bash
pip install -r requirements.txt
cp .env.example .env        # then paste your key after GEMINI_API_KEY=
python run.py
```

Open http://localhost:8000. Get a key at https://aistudio.google.com/apikey. Without one the app still starts; live AI is off and the demos work.

## Production SaaS mode

The repository now includes a production identity and billing foundation. Set `AUTH_REQUIRED=true`, `DATABASE_URL` to PostgreSQL, and a long random `JWT_SECRET`; run `alembic upgrade head` before starting the API. Authentication supports email/password verification and reset tokens, Google/GitHub OAuth callbacks, rotating HttpOnly access/refresh cookies, optional API keys, teams with owner/member roles, per-user monthly usage, audit logs, and signed Stripe subscription webhooks. Redis/Celery can be enabled with `QUEUE_ENABLED=true` and `REDIS_URL`.

For a complete local dependency stack:

```bash
docker compose up --build
```

Configure OAuth callback URLs as `{APP_BASE_URL}/api/auth/oauth/google/callback` and `{APP_BASE_URL}/api/auth/oauth/github/callback`. When `EMAIL_FROM` is empty, signup and password-reset responses return development tokens for local testing; configure a mail provider before production use.

Render deployment is described by `render.yaml`. Create the Blueprint, enter the secret environment variables, and use the same `JWT_SECRET` on both the web and worker services. The web service runs Alembic migrations through the container entrypoint; the worker executes queued agent runs through Celery.

Detailed references:

- [Architecture](docs/ARCHITECTURE.md): components, data model, request flows, security boundaries, and deployment topology.
- [Operations](docs/OPERATIONS.md): local profiles, migrations, provider setup, health checks, testing, and Render rollout.
- [API reference](docs/API.md): authentication, workspace, run, approval, usage, billing, and admin routes.

With Docker:

```bash
GEMINI_API_KEY=your-key docker compose up --build
```

## Deploy it publicly

RepoPilot is a single container. On any host that runs Docker (Render, Fly.io, Railway, a VPS, Cloud Run):

1. Build from the included `Dockerfile` and expose port 8000.
2. Add **`GEMINI_API_KEY` as a secret environment variable** in the host's dashboard. Do not put it in a file in the repo.
3. Mount a persistent volume at `/data` (optional; without it, workspaces are lost on restart and users are told to re-add their repo).
4. **Serve over HTTPS** (most hosts do this for you) and, if you run behind a reverse proxy, set `TRUST_PROXY=true` so per-IP limits use real client IPs.
5. Keep `PUBLIC_MODE=true` (the default).

I couldn't deploy it for you from here, and the Docker files were not built in my sandbox, so treat the first deploy as something to check.

### What a public deployment costs you

Every live run spends **your** Gemini quota. These defaults bound it (all configurable in `.env.example`):

| Limit       | Default                                                                        |
| ----------- | ------------------------------------------------------------------------------ |
| Per run     | 30 steps and about $0.25 estimated                                             |
| Per IP      | 6 live runs per hour                                                           |
| Whole site  | $5 estimated per rolling 24 hours, then live runs pause and demos keep working |
| Per visitor | 5 repositories, deleted after 24 hours                                         |

Cost figures are **estimates** from token counts (thinking tokens bill as output). Defaults use a conservative rate; Google has listed lower introductory prices for some Flash models, so real spend is likely lower. Check your Google AI Studio billing, and set a budget alert there too. Free-tier Gemini usage may be used by Google to improve its products, so on a public site use a paid-tier key if visitors will load private code.

## What a visitor does

1. Open the page. The header shows whether the live AI is on.
2. Click a demo (for example _Pagination returns the wrong page_), or paste `https://github.com/owner/repo`. Pasted forms like `/tree/main/...` or `git@github.com:o/r.git` are normalized. An API key pasted into this box is rejected.
3. Press **Run agent**. Watch the timeline: plan, code search, file reads, then amber **approval gates** showing the exact diff or command.
4. Approve or reject each gate (with an optional note). Rejection is fed back to the model, which tries something else or stops.
5. Read the result: tests passing/failing/not run, files and lines changed, estimated cost, the diff. Download the patch, copy it, or push a branch and open a PR (this asks for the visitor's own GitHub token, used once and never stored).

Approval modes: **Ask me every time**, or **Auto-approve confident edits** (only edits the model rates at or above `AUTO_CONFIDENCE_THRESHOLD`; never protected files, never anything unsure).

## Security model

**The Gemini key**

- Read from the environment, hidden from `repr()`, never returned by any endpoint (tests check every response and the whole data directory for it).
- Model output, tool output and error text are scrubbed for anything key-shaped before being stored or shown.
- Test subprocesses and git run with a scrubbed environment, so a repository's tests cannot read it.
- The scanner `scripts/check_no_secrets.py` (CI and tests) fails if a key or `.env` file is ever committed.

**Ownership and authentication**

- In demo mode, an HttpOnly SameSite cookie holds a random 128-bit anonymous owner ID.
- In SaaS mode, access and refresh cookies identify a PostgreSQL user; API keys support CI access.
- Every session, run, approval, diff, patch, and usage operation is checked against the resolved owner ID.
- Foreign resources intentionally return `404` so tenants cannot enumerate one another's work.

**The agent** (hard blocks, refused before a human is asked, in every mode)

- Paths outside the repo: absolute, `..`, NUL, drive letters, escaping symlinks.
- Reading or writing secrets: `.env*`, `*.pem`, `*.key`, `id_rsa*`, `secrets.*`, `credentials*`, `.npmrc`, anything in `.git`. Secret files are also excluded from search so they can't reach the model.
- Any command that isn't a known test runner (`pytest`, `python -m pytest/unittest`, `npm/yarn/pnpm test`, `go test`, `cargo test`, `mvn test`, `gradle test`, `make test`). No shell, no operators, no `-c`/`-p`/`--rootdir`.
- Budgets: steps, cost, files changed, lines changed, bytes per write, test timeout.
- In auto mode a human is still asked for protected files (CI, Dockerfiles, dependency manifests) and low-confidence edits.

The system prompt tells the model that repository contents are data, not instructions. That reduces prompt-injection risk but is not a guarantee, which is why the hard blocks and approval gates exist independently of the model.

## What `PUBLIC_MODE` means for real repositories

Running a stranger's test suite means running a stranger's code on your server. With `PUBLIC_MODE=true` (default) RepoPilot therefore **does not execute tests for repositories other than the built-in demos**. On a real repo the agent can still search, read and propose edits, but it tells the user the change is **unverified**. The demos still run real tests.

To get real test verification, run RepoPilot on your own machine with `PUBLIC_MODE=false`. Doing that on a public server is not safe without a proper sandbox (containers or VMs per run), which this project does not include.

## Configuration

See `.env.example` for every setting. The important ones:

| Variable                              | Default               | Meaning                                                         |
| ------------------------------------- | --------------------- | --------------------------------------------------------------- |
| `GEMINI_API_KEY`                      | empty                 | The only secret. Empty means demo-only.                         |
| `GEMINI_MODEL`                        | `gemini-3.8-flash`    | Model used for live runs                                        |
| `GEMINI_THINKING_LEVEL`               | empty                 | `minimal`/`low`/`medium`/`high`, or empty for the model default |
| `PUBLIC_MODE`                         | `true`                | Don't run tests for non-demo repos                              |
| `DAILY_COST_CAP_USD` / `MAX_COST_USD` | `5.00` / `0.25`       | Site-wide daily and per-run spend limits                        |
| `RUNS_PER_IP_PER_HOUR`                | `6`                   | Live runs per IP                                                |
| `TRUST_PROXY`                         | `false`               | Use `X-Forwarded-For` (only behind a proxy you control)         |
| `DATABASE_URL` / `AUTH_REQUIRED`      | empty / `false`       | PostgreSQL identity database and authenticated production mode  |
| `JWT_SECRET`                          | empty                 | Signing key for short-lived access tokens                       |
| `REDIS_URL` / `QUEUE_ENABLED`         | local Redis / `false` | Celery broker and worker execution                              |
| `GOOGLE_*` / `GITHUB_*`               | empty                 | OAuth client credentials                                        |
| `EMAIL_FROM` / `SMTP_*`               | empty                 | Verification and password-reset email delivery                  |
| `STRIPE_*`                            | empty                 | Paid-plan webhook integration                                   |
| `FREE_*` / `PAID_*`                   | documented defaults   | Monthly usage and concurrency plan limits                       |
| `ADMIN_EMAILS`                        | empty                 | Comma-separated audit-log administrators                        |

## Project layout

```
run.py                 one-command launcher with plain-language startup errors
backend/app/
  main.py              FastAPI routes, auth middleware, ownership checks, usage and billing
  auth.py              users, OAuth identities, tokens, teams, quotas, audit, API keys
  db.py                PostgreSQL runtime store with SQLite test/dev fallback
  runner.py            sessions, background runs, approval broker, cleanup, branch/PR flow
  agent.py             the agent loop and system prompt
  tools.py             sandboxed tools (search, read, edit, run tests, diff)
  guardrails.py        path sandbox, blocklists, test allowlist, approval policy
  gitops.py            URL normalisation, clone, diff, branch, push, GitHub PR
  indexer.py           BM25 code search + language / test-command detection
  llm.py               Gemini adapter and the scripted demo model
  demo/tasks.py        three demo repos with real bugs and scripted fixes
frontend-react/        React + TypeScript + Tailwind source and production build
frontend/              built static bundle served by FastAPI in the container
backend/alembic/       PostgreSQL migrations for identity and runtime tables
docs/                  architecture, operations, and API references
render.yaml            Render web, worker, Redis, and PostgreSQL Blueprint
backend/tests/         automated tests
backend/evals/         offline safety / retrieval / agent evals
scripts/check_no_secrets.py
```

## Tests

```bash
cd backend
python -m pytest -q          # no network or key needed
python -m evals.run          # offline safety, retrieval and agent evals
python ../scripts/check_no_secrets.py
```

The Gemini adapter is tested with the real `google-genai` SDK against a fake Google server, so request building, function-call parsing, thought-signature replay and error mapping run through real code. I also ran the whole app end to end (browser UI, live agent loop, approvals, diff) against `scripts/fake_gemini.py`, a strict fake that rejects any turn missing the thought signature it issued. You can repeat that without a key:

```bash
python scripts/fake_gemini.py &
GEMINI_API_KEY=fake-key GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:9199 python run.py
```

Live Gemini availability depends on the configured key, model access, network, and quota. Check `/api/ai/status` after deployment; a configured key can still receive upstream `busy` or `quota` responses.

## Known limitations

- **No per-run OS/container sandbox.** See `PUBLIC_MODE`. Test execution is off for arbitrary repositories on public servers unless the account plan and deployment policy permit it.
- **Public repositories only**, up to `MAX_REPO_MB` (100 by default), `github.com` by default.
- **Push and open PR** needs a token with write access, which visitors usually don't have on someone else's repo. Downloading the patch always works.
- **Repos need their own dependencies to run tests** (when tests are enabled). RepoPilot installs nothing.
- **Rate-limit counters remain process-local.** Redis/Celery moves run execution off the web process, but a distributed rate-limit store is still a future hardening step.
- **Email delivery, OAuth, Stripe, and Redis require provider configuration.** The endpoints and wiring are present, but credentials and callback/webhook registration are deployment-specific.
- Search is lexical (BM25), not semantic.

## Provenance

Rebuilt from the design described in the original Repo-Pilot README (whose backend source could not be read), then switched from Anthropic to Gemini and opened to the public.
