# Fix: PWA login always fails with `invalid_oauth_state`

## Background

Installed (standalone) PWAs on iOS deterministically fail Avibe Cloud remote-access
login: after approving the avibe.bot consent screen, the user lands on
`/auth/callback` and gets `invalid_oauth_state` every time. Normal browsers work.

## Root cause (evidence-backed, from the master regression env)

Topology: the PWA and `/auth/callback` are both on the tunnel host
(`test-app.avibe.bot`); the authorize/token endpoints are on `avibe.bot` (parent
domain, cross-origin but same registrable domain → same-site).

Diagnostic logging on the callback (regression) showed, on every PWA failure:

```
cookie_parsed=True  cookie_state_rid=<A>  url_state_rid=<B>  url_state_valid=True   (A != B)
handshake_cookie_present=True  sec_fetch_site=same-site  ua=...iPhone OS 18_7...
```

So the handshake cookie **is** delivered and valid, and the callback URL's `state`
is **also** a valid token we signed — but they are **two different states**.

Why: iOS standalone PWAs open the cross-origin avibe.bot authorize page in a
separate in-app-browser context, while the PWA's main webview independently
re-mints its own `GET /` → state + handshake cookie. The cookie the callback reads
therefore belongs to a *different* `GET /` generation than the consent the user
actually approved. The existing check `cookie.state == url.state` is the wrong
invariant in this multi-context environment.

(An earlier hypothesis — the cookie was a session cookie dropped across the
excursion — was disproven by `handshake_cookie_present=True`. Adding `Max-Age`
only changed the symptom from "absent" to "present-but-stale".)

## Fix

Stop requiring `cookie.state == url.state`. Recover the PKCE secrets by the
**signature-verified URL state** instead:

- `GET /` (`_redirect_to_vibe_cloud_login`): generate `state` (signed, with random
  id `r`), `nonce`, `code_verifier`. Persist `{nonce, code_verifier, next}`
  **server-side keyed by `r`** (single-use, 5-min TTL), in addition to the existing
  cookie.
- `/auth/callback`: verify the URL `state` signature, then:
  - **cookie-first** — if the cookie is present and its state matches the URL state,
    use the cookie's secrets (unchanged strong per-browser binding for normal
    browsers);
  - **store-fallback** — otherwise look up the server-side handshake by the URL
    state's `r` (the iOS PWA / cookie-desync case);
  - if neither yields a record → existing one-shot retry, then the friendly
    re-login page.

Server-side store: per-file under `~/.avibe/runtime/oauth_handshakes/<r>.json`,
`0600`, single-use (deleted on read), pruned by TTL. Single UI process, so no
cross-process coordination needed; on-disk so an in-flight login survives a UI
restart.

## Security

The change does not weaken the real gate: **which identity may complete OAuth for
an instance is enforced by the avibe.bot backend** (`isEmailAuthorizedForInstance`);
the local instance only trusts backend-issued tokens (audience/issuer/nonce
checked at exchange). The `state` remains HMAC-signed and single-use, so it cannot
be forged or replayed. The cookie's state-equality was a defense-in-depth layer
that is unavailable (and counter-productive) in standalone PWAs.

Codex review (gpt-5.5, xhigh) hardening applied:

- **Store-fallback requires a valid same-origin handshake cookie to be present**
  (its state may differ — that's the PWA desync — but it must parse/verify). This
  proves the browser actually started a login on this instance, so a bare
  `code+state` callback URL can't be replayed in a browser that never did. The PWA
  always carries such a cookie, so the fix still works.
- **`pop_oauth_handshake` is atomic single-use** via `os.replace` to a unique
  private name before reading, so concurrent callbacks for one `rid` can't both
  consume the same record.

### Known limitation / follow-up

The fallback still binds finalization to *a* same-origin login attempt, not to the
*specific* browser tab that approved consent (the cookie/jar that would provide
that binding is exactly what desyncs on iOS). On an instance that authorizes
**multiple** identities, an attacker who holds a valid `code+state` for their own
authorized identity could induce a victim's browser (which has visited the
instance) to hit that callback and be logged in as the attacker — a login-CSRF.

Bounded today: Avibe instances are single-identity (one authorized email), so no
second identity exists to mount this. The complete fix, for when multi-identity
matters, is a same-origin binding the desync can't break: the login-start page
writes a random nonce to `localStorage`, the store keeps its hash, and a JS
finalizer on the callback page POSTs `{code, state, nonce}` — tracked as a
follow-up rather than blocking this PWA fix.

## Testing

- Unit (`tests/test_ui_remote_access_auth.py`):
  - new: valid-but-mismatched cookie + matching server-side record → callback
    completes via the store, using the record's verifier/nonce (not the stale
    cookie's).
  - new: login redirect persists the handshake cookie (`Max-Age`).
  - existing cookie-path / retry / legacy-state / sanitization cases stay green.
- Manual: iOS standalone PWA login on the master regression env (`test-app.avibe.bot`).

## Rollout

1. Deploy to the master regression env; confirm PWA login succeeds (and the
   `recovered via server-side handshake` info log fires).
2. codex review (auth-path change) → open PR.
