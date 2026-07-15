# Production Deploy Checklist

Practical, do-this-before-you-flip-it notes for pushing GRAND Explorer
(fluxapp) changes to the Railway production environment. Grouped by the
feature that introduced the requirement. Check each box as you go.

---

## Contributor signup / workbench auth (django-allauth email login)

Required before enabling public/contributor signup in production. The auth
system is django-allauth with mandatory email verification; several settings
that are harmless in dev will silently break email links or CSRF in prod.

- [ ] **Re-apply the Site domain in the PROD database.** The
  `django.contrib.sites` `Site` row (pk = `SITE_ID` = 1) is a **database row,
  not code**. Dev is set to `grandexplorer.org` / "GRAND Explorer", but the
  prod DB still defaults to `example.com` until changed there. allauth builds
  all email link text from this row. Apply via one-off shell:
  ```bash
  python manage.py shell -c "from django.contrib.sites.models import Site; \
from django.conf import settings; s=Site.objects.get(pk=settings.SITE_ID); \
s.domain='grandexplorer.org'; s.name='GRAND Explorer'; s.save()"
  ```
- [ ] **`ACCOUNT_DEFAULT_HTTP_PROTOCOL=https`** in the Railway env. Defaults to
  `http`; without this, emailed verification/reset links come out as
  `http://grandexplorer.org/...`.
- [ ] **`CSRF_TRUSTED_ORIGINS`** = full origins with scheme, e.g.
  `https://grandexplorer.org,https://www.grandexplorer.org`. Without it, HTTPS
  logins and form POSTs fail CSRF validation.
- [ ] **Exempt `/accounts/` from `SoftLaunchGateMiddleware`.** While
  `SITE_PASSWORD` is set (the soft-launch HTTP Basic gate), email-verification
  links opened in a fresh browser sit behind the shared gate and block new
  contributors from completing signup.
- [ ] **Authenticate `grandexplorer.org` in Brevo (SPF + DKIM DNS records)** so
  verification/notification mail from `no-reply@grandexplorer.org` isn't junked
  or blocked by recipients. The SMTP relay auth itself already works; this is
  the deliverability piece.

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
