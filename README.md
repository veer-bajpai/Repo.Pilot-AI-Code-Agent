# RepoPilot

An AI developer agent you stay in control of, powered by **Google Gemini**. Point it at a public GitHub repository, describe a bug or a small change, and it reads the code, makes a plan, edits files and (where allowed) runs the tests. It **stops for your approval before every edit and every test run**, then hands you a diff you can download or turn into a pull request.

- **Open to the public: no login, no visitor API key.** Anyone with the link can use it. The Gemini key lives on the server (an environment variable), never in this repository and never in the browser.
- **Visitors are isolated.** Each browser gets a private anonymous ID; nobody can see or control anyone else's repositories, runs or approvals.
- **Built to survive being public.** Per-run budgets, a rolling daily spend cap, per-IP run limits, capacity caps and automatic cleanup keep a shared deployment from draining your key or your disk.
- **Try it with no key at all.** Three built-in demos (each a real bug with failing tests) run a scripted fix through the same approval workflow, so the app is useful even before you add a Gemini key.

## Quick start

You need Python 3.10+ and git.

```bash
pip install -r requirements.txt
cp .env.example .env        # then paste your key after GEMINI_API_KEY=
python run.py
```

Open http://localhost:8000. Get a key at https://aistudio.google.com/apikey. Without one the app still starts; live AI is off and the demos work.

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

| Limit | Default |
|---|---|
| Per run | 30 steps and about $0.25 estimated |
| Per IP | 6 live runs per hour |
| Whole site | $5 estimated per rolling 24 hours, then live runs pause and demos keep working |
| Per visitor | 5 repositories, deleted after 24 hours |

Cost figures are **estimates** from token counts (thinking tokens bill as output). Defaults use a conservative rate; Google has listed lower introductory prices for some Flash models, so real spend is likely lower. Check your Google AI Studio billing, and set a budget alert there too. Free-tier Gemini usage may be used by Google to improve its products, so on a public site use a paid-tier key if visitors will load private code.

## What a visitor does

1. Open the page. The header shows whether the live AI is on.
2. Click a demo (for example *Pagination returns the wrong page*), or paste `https://github.com/owner/repo`. Pasted forms like `/tree/main/...` or `git@github.com:o/r.git` are normalized. An API key pasted into this box is rejected.
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

**Visitors**
- An HttpOnly, SameSite cookie holds a random 128-bit ID. Every session, run, approval, diff and patch is checked against it, and "not yours" looks identical to "doesn't exist".
- There is no account system. Clearing cookies means losing access to your repos (they expire in 24 hours anyway). This is isolation, not authentication.

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

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | empty | The only secret. Empty means demo-only. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model used for live runs |
| `GEMINI_THINKING_LEVEL` | empty | `minimal`/`low`/`medium`/`high`, or empty for the model default |
| `PUBLIC_MODE` | `true` | Don't run tests for non-demo repos |
| `DAILY_COST_CAP_USD` / `MAX_COST_USD` | `5.00` / `0.25` | Site-wide daily and per-run spend limits |
| `RUNS_PER_IP_PER_HOUR` | `6` | Live runs per IP |
| `TRUST_PROXY` | `false` | Use `X-Forwarded-For` (only behind a proxy you control) |

## Project layout

```
run.py                 one-command launcher with plain-language startup errors
backend/app/
  main.py              FastAPI routes, visitor cookie, ownership checks, abuse limits
  runner.py            sessions, background runs, approval broker, cleanup, branch/PR flow
  agent.py             the agent loop and system prompt
  tools.py             sandboxed tools (search, read, edit, run tests, diff)
  guardrails.py        path sandbox, blocklists, test allowlist, approval policy
  gitops.py            URL normalisation, clone, diff, branch, push, GitHub PR
  indexer.py           BM25 code search + language / test-command detection
  llm.py               Gemini adapter and the scripted demo model
  demo/tasks.py        three demo repos with real bugs and scripted fixes
frontend/              static HTML/CSS/JS, no build step
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

**It has not been run against Google's real Gemini API** (my build environment can't reach it), so do one live run after your first deploy.

## Known limitations

- **Live Gemini path unverified against Google itself** (see above). The first real run is the real test.
- **No sandbox.** See `PUBLIC_MODE`. Test execution is off for arbitrary repos on public servers.
- **Public repositories only**, up to `MAX_REPO_MB` (100 by default), `github.com` by default.
- **Push and open PR** needs a token with write access, which visitors usually don't have on someone else's repo. Downloading the patch always works.
- **Repos need their own dependencies to run tests** (when tests are enabled). RepoPilot installs nothing.
- **In-memory rate limits and single-process SQLite.** Fine for a demo or small team; restarting resets the per-IP counters, and it isn't a multi-instance service.
- Search is lexical (BM25), not semantic.

## Provenance

Rebuilt from the design described in the original Repo-Pilot README (whose backend source could not be read), then switched from Anthropic to Gemini and opened to the public.
