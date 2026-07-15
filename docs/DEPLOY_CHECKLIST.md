# Production Deploy Checklist

Practical, do-this-before-you-flip-it notes for pushing GRAND Explorer
(fluxapp) changes to the Railway production environment. Grouped by the
feature that introduced the requirement. Check each box as you go.

---

## Contributor signup / workbench auth (django-allauth email login)

Required before enabling public/contributor signup in production. The auth
system is django-allauth with mandatory email verification; several settings
that are harmless in dev will silently break email links or CSRF in prod.

- [x] **Email MUST use Brevo's HTTP API — Railway blocks outbound SMTP on ALL
  ports** (587, 2525, 465 all hang; discovered 2026-07-14). Symptom: any
  synchronous email send — e.g. allauth's verification email during
  login/signup — freezes the request until gunicorn kills the worker
  (`[CRITICAL] WORKER TIMEOUT` in the deploy logs). Never point prod at
  Django's SMTP backend. Set in the Railway env:
  ```
  EMAIL_BACKEND=anymail.backends.brevo.EmailBackend
  ANYMAIL_BREVO_API_KEY=<Brevo v3 API key>   # SMTP & API → API Keys — NOT the SMTP password
  ```
  The `EMAIL_HOST/PORT/...` SMTP vars are ignored while anymail is the backend.
  `EMAIL_TIMEOUT=10` is set in settings as defense-in-depth for any SMTP use
  elsewhere. *(Done + verified end-to-end 2026-07-14.)*
- [x] **Re-apply the Site domain in the PROD database.** The
  `django.contrib.sites` `Site` row (pk = `SITE_ID` = 1) is a **database row,
  not code** — a fresh prod DB defaults to `example.com`, and allauth builds
  all email link text from this row. Easiest fix: edit it in the live Django
  admin at `/admin/sites/site/` (domain `grandexplorer.org`, display name
  "GRAND Explorer"). Takes effect immediately, no redeploy. *(Done 2026-07-14.)*
- [x] **`ACCOUNT_DEFAULT_HTTP_PROTOCOL=https`** in the Railway env. Defaults to
  `http`; without this, emailed verification/reset links come out as
  `http://grandexplorer.org/...`. *(Set; verified via a real verification email.)*
- [x] **`CSRF_TRUSTED_ORIGINS`** = full origins with scheme, e.g.
  `https://grandexplorer.org,https://www.grandexplorer.org`. Without it, HTTPS
  logins and form POSTs fail CSRF validation.
- [x] **Exempt `/accounts/` from `SoftLaunchGateMiddleware`.** *(Shipped in code
  2026-07-14 — `n2odb/middleware.py` skips the gate for `/accounts/` paths, so
  verification links opened in a fresh browser work while `SITE_PASSWORD` is
  set. Covered by `SoftLaunchGateTests`.)*
- [ ] **Authenticate `grandexplorer.org` in Brevo (SPF + DKIM DNS records)** so
  verification/notification mail from `no-reply@grandexplorer.org` isn't junked
  or blocked by recipients. Sending (via the API) already works; this is the
  deliverability piece.

## Shared cache (required for rate limiting to actually work)

- [ ] **Configure a shared `CACHES` backend** (Redis, Memcached, or the DB cache
  via `python manage.py createcachetable`). allauth's throttles
  (`ACCOUNT_RATE_LIMITS`: login, signup, password reset, email confirmation)
  store their counters in Django's cache. The default local-memory cache is
  **per-process**, so with multiple Railway workers each process keeps its own
  counters and the effective limits become N× looser than configured (and reset
  on every deploy/restart). A shared cache is what makes the login/signup/spam
  limits real in production.

## Security hardening (any HTTPS production deploy)

- [ ] `DEBUG=False`
- [ ] `ALLOWED_HOSTS` set to the real host(s)
- [ ] `SECURE_SSL_REDIRECT=True`
- [ ] `SESSION_COOKIE_SECURE=True`
- [ ] `CSRF_COOKIE_SECURE=True`
- [ ] HSTS: raise `SECURE_HSTS_SECONDS` from the soft-launch 300 to `31536000`
  once the domain is confirmed stable on HTTPS.

## Going fully public (lifting the soft-launch gate)

- [ ] Set up Railway DB backups (the data is the whole value).
- [ ] Add `grand_ro` SELECT-only DB user + `READONLY_DATABASE_URL` for SQL Explorer.
- [ ] Remove `SITE_PASSWORD` to lift the HTTP Basic gate.

## Manuscript LLM pre-fill (workbench)

- [ ] **`ANTHROPIC_API_KEY`** set in the Railway env — without it every
  manuscript extraction fails (`LLMUnavailable`).
- [ ] **`WORKBENCH_LLM_ENABLED=True`** — defaults to `False`, so the feature
  stays off even with a key.
- [ ] Smoke-test on the live site: upload a manuscript PDF in the workbench and
  confirm the pre-fill runs. *(Not yet verified in prod as of 2026-07-14.)*

## Optional tuning (no action required to deploy)

- [ ] **`WORKBENCH_LLM_DAILY_CAP`** — per-account daily ceiling on billable
  manuscript-extraction (LLM) calls. **Defaults to `10`; production works with no
  config.** Set the env var only to change it (e.g. `WORKBENCH_LLM_DAILY_CAP=25`,
  or `0` to disable the cap). Staff accounts are always exempt. Unlike the allauth
  throttles above, this counter is **DB-backed** (`LlmExtractionCall` rows), so it is
  **independent of the shared-cache item** — it holds across Railway workers and
  restarts either way. Usage is visible under Admin → *LLM extraction calls*.

---

*Ops note:* run `manage.py` **inside** the app container; run `docker`/`compose`
on the host. For this repo, run git **through VSCode (in-container)** — host/
distrobox git hits `.git/objects` ownership splits.
