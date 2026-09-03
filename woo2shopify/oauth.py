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
import re
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

# Credentials that look like an Admin API token but are not one. Shopify answers
# all of them with the same opaque 401, so name them up front instead.
WRONG_TOKEN_PREFIXES = {
    "shpss_": (
        "an app Client secret. Secrets are exchanged for a token, not used as one — "
        "put it in the Client secret field and press Mint token."
    ),
    "atkn_": (
        "an App Automation Token. Those authenticate Shopify CLI for deploying app "
        "versions in CI/CD and cannot call the Admin API at all — no store data, no "
        "customers, no orders. Use the app's Client ID and secret with Mint token instead."
    ),
    "shpca_": "a Customer Account API token, which cannot read Admin API data.",
    "shppa_": "a Partner API token, which only reaches the Partner API.",
    "prtapi_": "a Partner API token, which only reaches the Partner API.",
}


def describe_token_problem(token: str) -> str:
    """Name a recognisably wrong credential, or '' if it could be a real token."""
    value = (token or "").strip()
    for prefix, what in WRONG_TOKEN_PREFIXES.items():
        if value.startswith(prefix):
            return f"That token starts with '{prefix}', so it is {what}"
    return ""
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
    grant = read_grant(response.json())
    grant["expires_in"] = int(grant["expires_in"]) or 86399
    return grant


def clean_error_body(body: str) -> str:
    """Shopify serves OAuth failures as HTML pages; keep only the useful part."""
    text = body or ""
    if "<html" in text.lower():
        match = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        return (match.group(1).strip() if match else "HTML error page")[:200]
    return text[:300]


def _explain_grant_failure(status: int, body: str) -> str:
    detail = clean_error_body(body)
    if "shop_not_permitted" in (body or ""):
        return (
            "Shopify says shop_not_permitted: the client credentials grant is not allowed on "
            "this store.\n"
            "That grant only reaches development stores created in the Dev Dashboard under the "
            "same organization as the app. A paid or trial store, or a store from another "
            "organization, always fails this way — no app setting changes it.\n"
            "Use the browser OAuth flow (authorization code grant) instead:\n"
            "  1. Set the app's distribution to Custom distribution for this store.\n"
            f"  2. Add {redirect_uri()} to the app's allowed redirect URLs (and set the app URL "
            "to the same host if Shopify insists they match).\n"
            "  3. Press 'Browser OAuth instead' and approve the install."
        )
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


def read_grant(payload: Dict[str, object]) -> Dict[str, object]:
    """Normalise a token response.

    Shopify may return a permanent offline token (no expiry) or an expiring one
    with a refresh token. Both are accepted; `expires_in` of 0 means the token
    does not expire.
    """
    token = payload.get("access_token")
    if not token:
        raise OAuthError(f"No access_token in the response: {str(payload)[:300]}")
    return {
        "access_token": str(token),
        "scope": str(payload.get("scope") or ""),
        "expires_in": int(payload.get("expires_in") or 0),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "refresh_token_expires_in": int(payload.get("refresh_token_expires_in") or 0),
    }


def refresh_access_token(
    shop_domain: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    log: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, object]:
    """Trade a refresh token for a fresh access token."""
    log = log or (lambda message, level="info": None)
    shop = normalize_shop(shop_domain)
    if not refresh_token:
        raise OAuthError("No refresh token stored — run the browser OAuth flow again.")
    log("Refreshing the Shopify access token…", "info")
    response = requests.post(
        f"https://{shop}{TOKEN_PATH}",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise OAuthError(
            f"Token refresh failed (HTTP {response.status_code}): {clean_error_body(response.text)}\n"
            "Refresh tokens last 90 days — if it has lapsed, run the browser OAuth flow again."
        )
    return read_grant(response.json())


def apply_grant(shopify_config, grant: Dict[str, object], auth_mode: str = "") -> None:
    """Write a token response into a ShopifyConfig, expiries included."""
    now = time.time()
    shopify_config.access_token = str(grant["access_token"])
    expires_in = int(grant.get("expires_in") or 0)
    shopify_config.token_expires_at = now + expires_in if expires_in else 0.0
    refresh = str(grant.get("refresh_token") or "")
    if refresh:
        shopify_config.refresh_token = refresh
        refresh_ttl = int(grant.get("refresh_token_expires_in") or 0)
        shopify_config.refresh_token_expires_at = now + refresh_ttl if refresh_ttl else 0.0
    if auth_mode:
        shopify_config.auth_mode = auth_mode


class TokenSource:
    """Supplies the Admin API token, renewing it however this config allows.

    Three cases, because Shopify has three:
      * client credentials — mint a fresh 24-hour token whenever one is needed;
      * an expiring token plus a refresh token (authorization code grant) —
        refresh before it lapses, and again if a request comes back 401;
      * a plain long-lived token — hand it over as-is.

    A renewed token is written back to the config, so a migration resumed
    tomorrow does not need the browser flow again.
    """

    def __init__(self, config, log: Optional[Callable[[str, str], None]] = None, save=None):
        self.config = config
        self._log = log or (lambda message, level="info": None)
        self._save = save
        self._lock = threading.Lock()
        self._minted = ""
        self._minted_expires_at = 0.0
        self._scope = ""

    @property
    def uses_client_credentials(self) -> bool:
        cfg = self.config
        return bool(
            getattr(cfg, "auth_mode", "token") == "client_credentials"
            and cfg.client_id
            and cfg.client_secret
        )

    @property
    def uses_refresh_token(self) -> bool:
        cfg = self.config
        return bool(
            not self.uses_client_credentials
            and getattr(cfg, "refresh_token", "")
            and cfg.client_id
            and cfg.client_secret
        )

    @property
    def can_refresh(self) -> bool:
        return self.uses_client_credentials or self.uses_refresh_token

    @property
    def scope(self) -> str:
        return self._scope

    def token(self, force: bool = False) -> str:
        with self._lock:
            if self.uses_client_credentials:
                return self._client_credentials_token(force)
            if self.uses_refresh_token:
                return self._refreshed_token(force)
            return self._static_token()

    # ------------------------------------------------------------- internals
    def _client_credentials_token(self, force: bool) -> str:
        if self._minted and time.time() < self._minted_expires_at and not force:
            return self._minted
        grant = fetch_client_credentials_token(
            self.config.shop_domain, self.config.client_id, self.config.client_secret,
            log=self._log,
        )
        self._minted = str(grant["access_token"])
        self._scope = str(grant["scope"])
        self._minted_expires_at = time.time() + max(int(grant["expires_in"]) - REFRESH_MARGIN, 30)
        self._log(
            f"Access token minted, valid ~{int(grant['expires_in']) // 3600}h "
            f"(scopes: {self._scope or 'as configured on the app'})",
            "success",
        )
        return self._minted

    def _refreshed_token(self, force: bool) -> str:
        cfg = self.config
        expires_at = float(getattr(cfg, "token_expires_at", 0.0) or 0.0)
        fresh_enough = cfg.access_token and (
            expires_at == 0 or time.time() < expires_at - REFRESH_MARGIN
        )
        if fresh_enough and not force:
            return cfg.access_token

        refresh_expiry = float(getattr(cfg, "refresh_token_expires_at", 0.0) or 0.0)
        if refresh_expiry and time.time() > refresh_expiry:
            raise OAuthError(
                "The stored refresh token has expired (they last 90 days). Run the browser "
                "OAuth flow again to get a new one."
            )
        grant = refresh_access_token(
            cfg.shop_domain, cfg.client_id, cfg.client_secret, cfg.refresh_token, log=self._log,
        )
        apply_grant(cfg, grant)
        self._scope = str(grant["scope"])
        if self._save:
            try:
                self._save()
            except Exception as exc:
                self._log(f"Token refreshed but could not be saved: {exc}", "warn")
        self._log("Access token refreshed.", "success")
        return cfg.access_token

    def _static_token(self) -> str:
        cfg = self.config
        if not cfg.access_token:
            raise OAuthError(
                "No Shopify Admin API token configured. Either paste an access token, or enter "
                "the app's Client ID and secret and mint one."
            )
        problem = describe_token_problem(cfg.access_token)
        if problem:
            raise OAuthError(problem)
        expires_at = float(getattr(cfg, "token_expires_at", 0.0) or 0.0)
        if expires_at and time.time() > expires_at:
            raise OAuthError(
                "The stored access token has expired and there is no refresh token to renew it. "
                "Get a new one from the app credentials."
            )
        return cfg.access_token


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
    timeout: int = 600,
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
        log("If the browser did not open, paste the URL above into it by hand.", "info")
        if open_browser:
            webbrowser.open(url)

        if not _CallbackHandler.done.wait(timeout):
            raise OAuthError(
                f"Timed out after {timeout}s waiting for Shopify to call back.\n"
                "Shopify usually shows a page instead of redirecting when the app is not set "
                f"up for this: check that {redirect_uri(port)} is listed in the app's allowed "
                "redirect URLs (exactly, including the port) and that the app's distribution "
                "lets it be installed on this store."
            )
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
