# RepoPilot API Reference

All API routes are same-origin JSON routes. Browser requests use `credentials: include` so HttpOnly cookies are sent automatically. CI clients may use `X-API-Key: rp_...`.

## Public routes

| Method | Route                                 | Purpose                                          |
| ------ | ------------------------------------- | ------------------------------------------------ |
| `GET`  | `/api/health`                         | Process, Git, and Gemini configuration health    |
| `GET`  | `/api/config`                         | Frontend configuration, demos, limits, auth mode |
| `GET`  | `/api/ai/status`                      | Cached live Gemini validation                    |
| `POST` | `/api/auth/signup`                    | Create an email/password account                 |
| `POST` | `/api/auth/login`                     | Authenticate and issue cookies                   |
| `POST` | `/api/auth/verify`                    | Consume an email verification token              |
| `POST` | `/api/auth/password-reset/request`    | Request a reset token/email                      |
| `POST` | `/api/auth/password-reset/confirm`    | Set a new password                               |
| `POST` | `/api/auth/refresh`                   | Rotate refresh token and access cookie           |
| `POST` | `/api/auth/logout`                    | Revoke refresh token and clear cookies           |
| `GET`  | `/api/auth/oauth/{provider}`          | Start Google or GitHub OAuth                     |
| `GET`  | `/api/auth/oauth/{provider}/callback` | Complete OAuth callback                          |
| `POST` | `/api/billing/webhook`                | Verify and process Stripe events                 |

## Authenticated routes

| Method   | Route                             | Purpose                                                          |
| -------- | --------------------------------- | ---------------------------------------------------------------- |
| `GET`    | `/api/auth/me`                    | Current account; returns `401` in SaaS mode when unauthenticated |
| `POST`   | `/api/auth/api-keys`              | Create a one-time-display CI API key                             |
| `GET`    | `/api/usage`                      | Current plan, monthly run limit, count, and estimated cost       |
| `POST`   | `/api/teams`                      | Create a team with the caller as owner                           |
| `GET`    | `/api/admin/audit`                | Admin audit log, restricted by `ADMIN_EMAILS`                    |
| `POST`   | `/api/sessions`                   | Add a demo, local folder, or public GitHub repository            |
| `GET`    | `/api/sessions`                   | List the caller's repositories/workspaces                        |
| `GET`    | `/api/sessions/{id}`              | Get one owned workspace and its run history                      |
| `DELETE` | `/api/sessions/{id}`              | Delete an owned workspace                                        |
| `GET`    | `/api/sessions/{id}/search?q=...` | Search indexed repository files                                  |
| `POST`   | `/api/sessions/{id}/runs`         | Start a live or simulated agent run                              |
| `GET`    | `/api/runs/{id}`                  | Get run state and usage                                          |
| `GET`    | `/api/runs/{id}/events?after=0`   | Incrementally poll the agent timeline                            |
| `GET`    | `/api/runs/{id}/approvals`        | List persisted approval gates                                    |
| `POST`   | `/api/approvals/{id}`             | Approve or reject a pending gate                                 |
| `POST`   | `/api/runs/{id}/cancel`           | Cancel an active run                                             |
| `GET`    | `/api/runs/{id}/diff`             | Get structured diff summary and raw diff                         |
| `GET`    | `/api/runs/{id}/patch`            | Download a patch                                                 |
| `POST`   | `/api/runs/{id}/pr`               | Create a branch or push/open a GitHub PR                         |

## Authentication request examples

### Signup

```json
POST /api/auth/signup
{
  "email": "developer@example.com",
  "password": "at-least-12-characters",
  "name": "Developer"
}
```

### Login

```json
POST /api/auth/login
{
  "email": "developer@example.com",
  "password": "at-least-12-characters"
}
```

The response sets:

- `rp_access`: short-lived HttpOnly access JWT;
- `rp_refresh`: rotating HttpOnly refresh token scoped to `/api/auth`.

### API key

```json
POST /api/auth/api-keys
{
  "name": "CI"
}
```

Use the returned key immediately; it is not returned again:

```text
X-API-Key: rp_<secret>
```

## Run request

```json
POST /api/sessions/{session_id}/runs
{
  "task": "Fix the pagination bug and make the tests pass.",
  "approval_mode": "ask",
  "mode": "live"
}
```

`mode` may be `live` or `simulated`. Simulated mode is restricted to built-in demos. `ask` preserves approval gates; `auto` can auto-approve only actions allowed by the existing confidence and protection rules.

## Approval request

```json
POST /api/approvals/{approval_id}
{
  "approved": true,
  "note": "Reviewed the diff"
}
```

Every authenticated decision creates an audit record with the decision and approval ID.

## Error semantics

- `401`: authentication required or invalid credentials;
- `403`: authenticated but not authorized for the operation;
- `404`: resource missing or owned by another tenant;
- `409`: duplicate account or conflicting active operation;
- `422`: invalid request data;
- `429`: usage, concurrency, IP, or capacity limit;
- `503`: provider or integration not configured.

Resource ownership intentionally uses the same `404` for missing and foreign resources so tenants cannot enumerate one another's repositories, runs, approvals, diffs, or patches.
