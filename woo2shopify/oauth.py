"""Turning a Shopify app's Client ID/secret into an Admin API access token.

Two flows, because Shopify has two situations:

* `fetch_client_credentials_token` — the client credentials grant. One POST, no
  browser, no redirect URL. Requires the app and the store to sit in the same
  Shopify organization, which is the case for an app you built in the Dev
  Dashboard and installed on your own store. The token it returns lasts 24
  hours, so `TokenSource` below re-mints it as needed.
* `fetch_offline_token` — the classic authorization-code flow through the
  browser. Needed when the app is not in the store's organization (a client's
  store, a distributed app). Returns a long-lived offline token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
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


TOKEN_PATH = "/admin/oauth/access_token"
REFRESH_MARGIN = 120  # re-mint this many seconds before the token actually dies


def normalize_shop(shop_domain: str) -> str:
    """'https://My-Store.myshopify.com/' -> 'my-store.myshopify.com'."""
    shop = (shop_domain or "").strip().lower()
    for prefix in ("https://", "http://"):
        if shop.startswith(prefix):
            shop = shop[len(prefix):]
    shop = shop.strip("/").split("/")[0]
    if shop.endswith(".myshopify.com"):
        return shop
    if shop and "." not in shop:
        return f"{shop}.myshopify.com"       # bare handle, e.g. 'xzpcy1-7w'
    if shop.startswith("admin.shopify.com"):
        raise OAuthError(
            "Use the myshopify domain, not the admin URL: for "
            "admin.shopify.com/store/my-store that is my-store.myshopify.com"
        )
    return shop


def fetch_client_credentials_token(
    shop_domain: str,
    client_id: str,
    client_secret: str,
    log: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, object]:
    """Client credentials grant — returns {'access_token', 'scope', 'expires_in'}.

    Works when the app and the store belong to the same Shopify organization.
    The app must be installed on the store and its version must declare the
    scopes; the grant hands back exactly those scopes.
    """
    log = log or (lambda message, level="info": None)
    shop = normalize_shop(shop_domain)
    if not shop.endswith(".myshopify.com"):
        raise OAuthError(f"'{shop_domain}' is not a myshopify domain (expected my-store.myshopify.com)")
    if not (client_id and client_secret):
        raise OAuthError("Client ID and Client secret are both required")

    log(f"Requesting an access token from {shop} (client credentials grant)…", "info")
    response = requests.post(
        f"https://{shop}{TOKEN_PATH}",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise OAuthError(_explain_grant_failure(response.status_code, response.text))
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise OAuthError(f"No access_token in the response: {str(payload)[:300]}")
    return {
        "access_token": token,
        "scope": payload.get("scope", ""),
        "expires_in": int(payload.get("expires_in") or 86399),
    }


def _explain_grant_failure(status: int, body: str) -> str:
    detail = body[:300]
    if status in (400, 401, 403):
        return (
            f"Shopify refused the client credentials grant (HTTP {status}): {detail}\n"
            "Check that:\n"
            "  • the Client ID and secret are from this app and the secret has not been rotated,\n"
            "  • the app is installed on this store,\n"
            "  • the app and the store belong to the same Shopify organization — if not, use the\n"
            "    browser OAuth flow instead,\n"
            "  • the app version declares the scopes you need and has been released."
        )
    return f"Client credentials grant failed (HTTP {status}): {detail}"


class TokenSource:
    """Supplies the Admin API token, re-minting it when it is about to expire.

    Client-credentials tokens live 24 hours, which is shorter than a large
    migration, so the client asks this for a token on every request rather than
    caching one at startup.
    """

    def __init__(self, config, log: Optional[Callable[[str, str], None]] = None):
        self.config = config
        self._log = log or (lambda message, level="info": None)
        self._token = ""
        self._expires_at = 0.0
        self._scope = ""
        self._lock = threading.Lock()

    @property
    def uses_client_credentials(self) -> bool:
        cfg = self.config
        return bool(
            getattr(cfg, "auth_mode", "token") == "client_credentials"
            and cfg.client_id
            and cfg.client_secret
        )

    @property
    def can_refresh(self) -> bool:
        return self.uses_client_credentials

    @property
    def scope(self) -> str:
        return self._scope

    def token(self, force: bool = False) -> str:
        if not self.uses_client_credentials:
            if not self.config.access_token:
                raise OAuthError(
                    "No Shopify Admin API token configured. Either paste an access token, or "
                    "set the auth mode to 'client_credentials' and enter the app's Client ID "
                    "and secret."
                )
            return self.config.access_token

        with self._lock:
            fresh_enough = self._token and time.time() < self._expires_at
            if fresh_enough and not force:
                return self._token
            result = fetch_client_credentials_token(
                self.config.shop_domain, self.config.client_id, self.config.client_secret,
                log=self._log,
            )
            self._token = str(result["access_token"])
            self._scope = str(result["scope"])
            self._expires_at = time.time() + max(int(result["expires_in"]) - REFRESH_MARGIN, 30)
            self._log(
                f"Access token minted, valid ~{int(result['expires_in']) // 3600}h "
                f"(scopes: {self._scope or 'as configured on the app'})",
                "success",
            )
            return self._token


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
    shop = normalize_shop(shop_domain)
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
