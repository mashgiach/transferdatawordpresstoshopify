"""Exchange a Shopify app's Client ID/secret for an Admin API access token.

Apps created in the Dev/Partner Dashboard are OAuth apps: their Client ID and
Client secret are *not* Admin API tokens. This runs the standard authorization
flow against a local redirect URL and returns the offline `shpat_...` token the
rest of the tool uses.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

DEFAULT_SCOPES = "write_customers,write_orders,read_products,read_locations"
DEFAULT_PORT = 3456
CALLBACK_PATH = "/callback"

PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<body style="font-family:system-ui,sans-serif;background:#16181d;color:#e8e8e8;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center;max-width:520px">
<h2 style="color:{colour}">{title}</h2><p style="line-height:1.5">{message}</p></div>"""


class OAuthError(RuntimeError):
    pass


def redirect_uri(port: int = DEFAULT_PORT) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def _verify_hmac(query: Dict[str, list], client_secret: str) -> bool:
    received = (query.get("hmac") or [""])[0]
    if not received:
        return False
    pairs = sorted(
        f"{key}={values[0]}"
        for key, values in query.items()
        if key not in ("hmac", "signature")
    )
    digest = hmac.new(client_secret.encode(), "&".join(pairs).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, received)


class _CallbackHandler(BaseHTTPRequestHandler):
    result: Dict[str, str] = {}
    expected_state = ""
    client_secret = ""
    done = threading.Event()

    def log_message(self, *args):
        pass

    def _page(self, status: int, title: str, message: str, colour: str) -> None:
        body = PAGE.format(title=title, message=message, colour=colour).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != CALLBACK_PATH:
            self._page(404, "Not here", "Nothing to see on this path.", "#e0a83c")
            return
        query = parse_qs(url.query)

        if (query.get("state") or [""])[0] != self.expected_state:
            self.__class__.result = {"error": "state mismatch — the callback did not come from your request"}
        elif not _verify_hmac(query, self.client_secret):
            self.__class__.result = {"error": "HMAC check failed — the Client secret does not match this app"}
        elif not (query.get("code") or [""])[0]:
            self.__class__.result = {"error": (query.get("error_description") or query.get("error") or ["no code returned"])[0]}
        else:
            self.__class__.result = {"code": query["code"][0], "shop": (query.get("shop") or [""])[0]}

        if "error" in self.__class__.result:
            self._page(400, "Authorization failed", self.__class__.result["error"], "#ef6f6f")
        else:
            self._page(200, "Connected", "Token received. You can close this tab and go back to the app.", "#5fd08a")
        self.__class__.done.set()


def fetch_offline_token(
    shop_domain: str,
    client_id: str,
    client_secret: str,
    scopes: str = DEFAULT_SCOPES,
    port: int = DEFAULT_PORT,
    timeout: int = 300,
    open_browser: bool = True,
    log: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, str]:
    """Run the install/authorize flow and return {'access_token', 'scope', 'shop'}."""
    log = log or (lambda message, level="info": None)
    shop = (shop_domain or "").strip().replace("https://", "").replace("http://", "").strip("/")
    if not shop.endswith(".myshopify.com"):
        raise OAuthError(f"'{shop_domain}' is not a myshopify domain (expected my-store.myshopify.com)")
    if not (client_id and client_secret):
        raise OAuthError("Client ID and Client secret are both required")

    state = secrets.token_urlsafe(24)
    try:
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise OAuthError(f"Cannot listen on port {port} ({exc}). Close whatever is using it or pick another port.")

    # armed only once the socket is bound, so nothing can answer a stale flow
    _CallbackHandler.result = {}
    _CallbackHandler.client_secret = client_secret
    _CallbackHandler.done = threading.Event()
    _CallbackHandler.expected_state = state

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"https://{shop}/admin/oauth/authorize?" + urlencode({
            "client_id": client_id,
            "scope": scopes,
            "redirect_uri": redirect_uri(port),
            "state": state,
        })
        log(f"Opening {url}", "info")
        log(f"The app's redirect URL must include exactly: {redirect_uri(port)}", "info")
        if open_browser:
            webbrowser.open(url)

        if not _CallbackHandler.done.wait(timeout):
            raise OAuthError(f"Timed out after {timeout}s waiting for Shopify to call back")
        if "error" in _CallbackHandler.result:
            raise OAuthError(_CallbackHandler.result["error"])
        code = _CallbackHandler.result["code"]
    finally:
        server.shutdown()
        server.server_close()

    log("Exchanging the authorization code for an access token…", "info")
    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": client_id, "client_secret": client_secret, "code": code},
        timeout=30,
    )
    if response.status_code >= 400:
        raise OAuthError(f"Token exchange failed (HTTP {response.status_code}): {response.text[:300]}")
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise OAuthError(f"No access_token in the response: {str(payload)[:300]}")
    return {"access_token": token, "scope": payload.get("scope", ""), "shop": shop}
