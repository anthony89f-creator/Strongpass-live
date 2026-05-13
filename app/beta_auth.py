"""
Temporary beta-access gate for StrongPass.

Wraps the entire site behind a shared password while in development/beta.
Removal path:
  1. Delete this file
  2. Remove `register_beta_auth(app)` from server.py (one line)
  3. Remove BETA_TOKEN / SECRET_KEY from strongman.service

Disable without a deploy: unset BETA_TOKEN in strongman.service and restart.
"""

import os
import hmac
from datetime import timedelta
from flask import request, redirect, make_response, render_template_string, session

# Paths that never require authentication.
_BYPASS = frozenset({"/beta/login", "/beta/logout", "/robots.txt", "/favicon.ico"})

_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>StrongPass — Beta Access</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: #0d0d0d;
      color: #e0e0e0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }
    .card {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 10px;
      padding: 2.5rem 2rem;
      width: 100%;
      max-width: 360px;
    }
    .wordmark {
      text-align: center;
      font-size: 1.5rem;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 0.4rem;
    }
    .wordmark span { color: #cc0033; }
    .subtitle {
      text-align: center;
      font-size: 0.75rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: #555;
      margin-bottom: 2rem;
    }
    label {
      display: block;
      font-size: 0.75rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: #888;
      margin-bottom: 0.5rem;
    }
    input[type="password"] {
      display: block;
      width: 100%;
      padding: 0.7rem 0.85rem;
      background: #111;
      border: 1px solid #333;
      border-radius: 5px;
      color: #eee;
      font-size: 1rem;
      outline: none;
      transition: border-color 0.15s;
    }
    input[type="password"]:focus { border-color: #555; }
    input[type="password"].err { border-color: #cc3333; }
    .error-msg {
      margin-top: 0.5rem;
      font-size: 0.8rem;
      color: #e05555;
    }
    button {
      display: block;
      width: 100%;
      margin-top: 1.25rem;
      padding: 0.75rem;
      background: #cc0033;
      border: none;
      border-radius: 5px;
      color: #fff;
      font-size: 0.9rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 0.15s;
    }
    button:hover { background: #e5003a; }
    button:active { background: #aa0028; }
    .footer {
      margin-top: 1.75rem;
      text-align: center;
      font-size: 0.7rem;
      color: #3a3a3a;
      letter-spacing: 0.05em;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="wordmark">Strong<span>Pass</span></div>
    <div class="subtitle">Beta Access</div>
    <form method="post" autocomplete="on" novalidate>
      <label for="token">Access Password</label>
      <input
        type="password"
        id="token"
        name="token"
        placeholder="Enter password"
        autofocus
        autocomplete="current-password"
        {% if error %}class="err"{% endif %}
      >
      {% if error %}<div class="error-msg">Incorrect password — try again.</div>{% endif %}
      <button type="submit">Enter</button>
    </form>
    <div class="footer">COMPETITION OS &mdash; BETA</div>
  </div>
</body>
</html>"""


def _token() -> str:
    return os.environ.get("BETA_TOKEN", "").strip()


def _secret() -> str:
    """Derive a secret key. Prefer an explicit SECRET_KEY env var; fall back to token-derived value."""
    return os.environ.get("SECRET_KEY", "").strip() or f"__sp_beta__{_token()}"


def _session_valid() -> bool:
    return session.get("beta_ok") is True


def _url_token_valid() -> bool:
    """Allow ?token= for OBS browser sources that cannot use cookies."""
    tok = _token()
    param = request.args.get("token", "")
    return bool(tok and param and hmac.compare_digest(param, tok))


def register_beta_auth(app) -> None:
    """
    Register the beta gate on the Flask app. No-op if BETA_TOKEN is not set.

    Must be called before any other before_request hooks are registered so
    that the gate runs first in the hook chain.
    """
    if not _token():
        return

    app.secret_key = _secret()
    app.permanent_session_lifetime = timedelta(days=7)
    app.config.update(
        SESSION_COOKIE_NAME="sp_beta",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,   # HTTPS only — correct for production
        SESSION_COOKIE_SAMESITE="Lax",
    )

    # ── Routes ─────────────────────────────────────────────────────────────────

    @app.route("/beta/login", methods=["GET", "POST"])
    def beta_login():
        err = False
        raw_next = request.args.get("next", "/")
        # Prevent open redirect — only accept absolute paths on this host
        next_url = raw_next if (raw_next.startswith("/") and not raw_next.startswith("//")) else "/"
        if request.method == "POST":
            submitted = request.form.get("token", "")
            if hmac.compare_digest(submitted, _token()):
                session.permanent = True
                session["beta_ok"] = True
                return redirect(next_url)
            err = True
        resp = make_response(render_template_string(_LOGIN_HTML, error=err))
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/beta/logout")
    def beta_logout():
        session.clear()
        return redirect("/beta/login")

    @app.route("/robots.txt")
    def robots_txt():
        return app.response_class(
            "User-agent: *\nDisallow: /\n",
            mimetype="text/plain",
        )

    # ── Hooks ──────────────────────────────────────────────────────────────────

    @app.before_request
    def _beta_gate():
        # Always allow CORS preflights — they carry no cookies.
        if request.method == "OPTIONS":
            return

        if request.path in _BYPASS:
            return

        # Already authenticated via session cookie.
        if _session_valid():
            return

        # ?token= in URL — used by OBS browser sources. Sets a session cookie so
        # subsequent requests from the same OBS browser profile don't need the param.
        if _url_token_valid():
            session.permanent = True
            session["beta_ok"] = True
            return

        # Non-browser clients (SSE consumers, fetch() on unauthenticated pages,
        # API callers) receive 401 rather than an HTML redirect.
        accept = request.headers.get("Accept", "")
        if "text/html" not in accept:
            return app.response_class("Unauthorized\n", status=401, mimetype="text/plain")

        return redirect(f"/beta/login?next={request.path}")

    @app.after_request
    def _noindex_header(response):
        """Tell crawlers not to index anything while in beta."""
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
