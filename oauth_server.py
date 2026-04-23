"""Minimal OAuth 2.1 Authorization Server for MCP.

Implements Authorization Code + PKCE flow so Claude (claude.ai, Claude Desktop)
can connect to our MCP server as a remote tool.

Endpoints:
    GET  /authorize              — Authorization page (user enters API key)
    POST /authorize              — Validates key, returns auth code
    POST /token                  — Exchanges auth code for access token
    GET  /.well-known/oauth-authorization-server  — Discovery metadata

Validates against the `api_keys` table in Postgres.
Runs behind Caddy at https://mcp.legal-reengineering.com/oauth/

Usage:
    PYTHONPATH=/srv/caselaw .venv/bin/uvicorn oauth_server:app --host 0.0.0.0 --port 8002
"""
from __future__ import annotations

import hashlib
import base64
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Form, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from search_stack.parag.pg_conn import get_pg_url, _load_env
import psycopg

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("oauth")

app = FastAPI(title="PA-RAG OAuth Server", docs_url=None, redoc_url=None)

ISSUER = "https://mcp.legal-reengineering.com"

# In-memory stores (short-lived, OK for single-instance)
_auth_codes: dict[str, dict] = {}  # code → {api_key, client_id, redirect_uri, code_challenge, expires}
_access_tokens: dict[str, dict] = {}  # token → {api_key, name, scopes, expires}

CODE_TTL = 300       # 5 min
TOKEN_TTL = 86400    # 24h


def _get_pg():
    _load_env()
    return psycopg.connect(get_pg_url(), autocommit=True)


def _validate_api_key(key: str) -> dict | None:
    try:
        conn = _get_pg()
        row = conn.execute(
            "SELECT name, scopes FROM api_keys WHERE key_id = %s AND is_active",
            (key,),
        ).fetchone()
        conn.close()
        if row:
            return {"name": row[0], "scopes": row[1]}
    except Exception as e:
        log.error("DB error: %s", e)
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@app.get("/.well-known/oauth-authorization-server")
def discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PA-RAG — Authorize</title>
    <style>
        body { font-family: system-ui; max-width: 420px; margin: 60px auto; padding: 20px; }
        h1 { font-size: 1.4em; }
        input[type=text], input[type=password] { width: 100%%; padding: 10px; margin: 8px 0; box-sizing: border-box; font-size: 1em; border: 1px solid #ccc; border-radius: 4px; }
        button { width: 100%%; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 4px; font-size: 1em; cursor: pointer; margin-top: 12px; }
        button:hover { background: #1d4ed8; }
        .error { color: #dc2626; margin: 10px 0; }
        .info { color: #666; font-size: 0.9em; margin-top: 16px; }
    </style>
</head>
<body>
    <h1>PA-RAG Swiss Case Law</h1>
    <p>Authorize access to the legal research API.</p>
    %(error)s
    <form method="POST">
        <input type="password" name="api_key" placeholder="API Key (parag_sk_...)" required>
        <input type="hidden" name="client_id" value="%(client_id)s">
        <input type="hidden" name="redirect_uri" value="%(redirect_uri)s">
        <input type="hidden" name="state" value="%(state)s">
        <input type="hidden" name="code_challenge" value="%(code_challenge)s">
        <input type="hidden" name="code_challenge_method" value="%(code_challenge_method)s">
        <button type="submit">Authorize</button>
    </form>
    <p class="info">Enter your API key to grant Claude access to Swiss case law search, citations, and legal analysis.</p>
</body>
</html>"""


@app.get("/authorize", response_class=HTMLResponse)
def authorize_get(
    client_id: str = Query(""),
    redirect_uri: str = Query(""),
    response_type: str = Query("code"),
    state: str = Query(""),
    code_challenge: str = Query(""),
    code_challenge_method: str = Query("S256"),
):
    if response_type != "code":
        raise HTTPException(400, "Only response_type=code supported")
    return HTMLResponse(LOGIN_HTML % {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "error": "",
    })


@app.post("/authorize")
def authorize_post(
    api_key: str = Form(...),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
):
    user = _validate_api_key(api_key)
    if not user:
        return HTMLResponse(LOGIN_HTML % {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "error": '<p class="error">Invalid API key.</p>',
        }, status_code=200)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "api_key": api_key,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "expires": time.time() + CODE_TTL,
    }
    log.info("Auth code issued for %s (client=%s)", user["name"], client_id)

    # Cleanup expired codes
    now = time.time()
    expired = [k for k, v in _auth_codes.items() if v["expires"] < now]
    for k in expired:
        del _auth_codes[k]

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@app.post("/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
):
    if grant_type != "authorization_code":
        raise HTTPException(400, detail="Unsupported grant_type")

    if code not in _auth_codes:
        raise HTTPException(400, detail="Invalid or expired authorization code")

    auth = _auth_codes.pop(code)

    if auth["expires"] < time.time():
        raise HTTPException(400, detail="Authorization code expired")

    # PKCE verification
    if auth["code_challenge"] and code_verifier:
        if auth["code_challenge_method"] == "S256":
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).rstrip(b"=").decode()
            if expected != auth["code_challenge"]:
                raise HTTPException(400, detail="PKCE verification failed")
        else:
            raise HTTPException(400, detail="Unsupported code_challenge_method")

    # Issue access token
    access_token = f"pat_{secrets.token_urlsafe(32)}"
    user = _validate_api_key(auth["api_key"])

    _access_tokens[access_token] = {
        "api_key": auth["api_key"],
        "name": user["name"] if user else "unknown",
        "scopes": user["scopes"] if user else [],
        "expires": time.time() + TOKEN_TTL,
    }

    log.info("Access token issued for %s", _access_tokens[access_token]["name"])

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
    })


# ---------------------------------------------------------------------------
# Token validation (called by MCP server)
# ---------------------------------------------------------------------------

def validate_bearer_token(token: str) -> dict | None:
    """Validate a Bearer token. Returns user info or None."""
    info = _access_tokens.get(token)
    if not info:
        return None
    if info["expires"] < time.time():
        del _access_tokens[token]
        return None
    return info


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "oauth"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("OAUTH_PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
