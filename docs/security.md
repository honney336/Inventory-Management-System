# Security review

Findings from a review of this codebase against itself: source inspection plus automated
probes (`scripts/security_probe.py`, `scripts/security_probe2.py`) run against a throwaway
database. Re-run them after any change.

```bash
python scripts/security_probe.py
python scripts/security_probe2.py
pytest tests -q            # 58 tests, includes the regressions for each fix below
```

## Fixed

| Severity | Issue | Evidence | Fix |
|---|---|---|---|
| Critical | Session forgery with the shipped `SECRET_KEY` | a cookie signed with `dev-only-change-me` was accepted as full administrator, including `/users` and `/settings` | the key is no longer a constant: it is read from the environment, or generated once and stored in `instance/secret_key` with mode 600 |
| Critical | Debug mode on by default | `GET /console` returned 200 on the dev server and `Debugger PIN: 135-441-715` appeared in the log, which is code execution on the host | `DEBUG` now defaults to `False`; enable deliberately with `FLASK_DEBUG=true` |
| High | No brute-force protection | 20 consecutive wrong passwords, no delay, no counter, no lockout | failed sign-ins are stored and both the account and the source address lock for 15 minutes after 5 failures; the correct password is refused while locked |
| Medium | SSRF through the webhook setting | an administrator could store `http://169.254.169.254/latest/meta-data/` and the server would call it | webhook URLs must be http or https, without credentials, and must resolve to a public address; loopback, private, link-local and reserved ranges are refused |
| Medium | Missing security headers | no `X-Content-Type-Options`, framing, referrer or content security policy | all pages now carry nosniff, SAMEORIGIN, same-origin, COOP, a permissions policy and a CSP with `frame-ancestors 'self'` and `object-src 'none'` |
| Medium | Unbounded file upload | `MAX_CONTENT_LENGTH` was never configured despite a 413 handler | uploads are capped at 5 MB by default (`MAX_UPLOAD_MB`) |
| Medium | First-run setup race | two concurrent first requests could both create an administrator | setup returns 403 once any user exists, and the first administrator insert also writes an `installed_at` marker whose unique key breaks the tie |
| Medium | No ceiling on quantities | a receipt of 999,999,999 units was accepted | `MAX_LINE_QUANTITY` (1,000,000) is enforced on receipts, issues and transfers |
| Low | CSV export formula injection | a cell starting with `=` was exported verbatim and would execute in a spreadsheet | cells beginning with `= + - @` are prefixed with an apostrophe |
| Low | Login timing reveals account existence | 3 ms for an unknown user against 98 ms for a wrong password | an unknown identity is verified against a dummy hash so both paths take the same time |
| Low | Audit log trusts `X-Forwarded-For` | any client could forge the address recorded against an action | the header is only read when `TRUST_PROXY_HEADERS=true` |

The session is also cleared before `login_user`, so nothing planted in it survives sign-in.

## Checked and not exploitable

| Area | Result |
|---|---|
| SQL injection | every value is bound. 8 payloads across 8 endpoints produced no database error, and a tautology did not widen results. The search box echoing the payload is reflection, not injection |
| Stored and reflected XSS | `<script>` in a product name, a movement note and the search box is escaped on render |
| CSRF | every state-changing route returns 400 without a token, including logout and the JSON API |
| Privilege escalation | staff get 403 on every admin and manager route, managers get 403 on user and settings routes, and destructive routes are admin-only |
| IDOR | no cross-record access found; records are looked up by primary key and filtered by the user's rights |
| Open redirect | `//evil.example`, `https://evil.example`, `/\evil` and `javascript:` are all rejected |
| Path traversal | a CSV upload named `../../../etc/passwd.csv` wrote nothing outside the app |
| Session fixation | values written before sign-in do not survive it |
| Cookie flags | `HttpOnly` and `SameSite=Lax` always; `Secure` forced on in production |
| Secrets in output | the API never returns password hashes; error pages show no tracebacks |

## Before putting this on a network

1. Set `SECRET_KEY` from a real secret: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Terminate TLS and leave `SESSION_COOKIE_SECURE` at its production default of `true`.
3. Change the `admin` password, or seed with `--no-demo` and use the setup page.
4. Only set `TRUST_PROXY_HEADERS=true` when a reverse proxy you control sets that header.
5. Only set `ALLOW_PRIVATE_WEBHOOKS=true` if the webhook must reach an internal address, and understand that it re-opens SSRF.
6. Keep `FLASK_DEBUG` unset. The Werkzeug console prints its PIN to the log and executes code on the host.

## Known limits

- The CSP allows `unsafe-inline` for scripts because templates carry small inline blocks.
  Tightening it needs nonces threaded through `base.html`.
- Login throttling counts against the address and the account name. A distributed attacker
  spread across many addresses is still slowed but not stopped; put a rate limiter in front of
  the app for that.
- The webhook guard resolves DNS once per send, so a hostname that resolves to a public address
  at check time and a private one at connect time (DNS rebinding) is not covered. Keep the
  webhook on a host you control, or use a proxy you operate.