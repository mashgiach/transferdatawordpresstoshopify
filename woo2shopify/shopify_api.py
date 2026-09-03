"""Shopify Admin API client (GraphQL first, REST fallback) with leaky-bucket
throttling, retries and the handful of queries/mutations this tool needs.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from .config import ShopifyConfig
from .oauth import OAuthError, TokenSource

RETRY_STATUS = {429, 500, 502, 503, 504}


class ShopifyError(RuntimeError):
    pass


AUTH_HELP = (
    "Shopify rejected the Admin API token. A Client ID or Client secret is not an access "
    "token — it has to be exchanged for one.\n"
    "  • App built in the Dev Dashboard and installed on your own store: set Auth mode to "
    "'client_credentials' on the Connections page and enter the app's Client ID and secret. "
    "The tool then mints tokens itself and re-mints them every 24 hours "
    "(CLI: python -m woo2shopify.cli token).\n"
    "  • App not in the store's Shopify organization (a client's store): use "
    "'Get token via browser OAuth' instead (CLI: python -m woo2shopify.cli oauth).\n"
    "  • An expired or rotated token, or scopes missing from the app version, produces this "
    "same 401 — re-release the app version after changing scopes.\n"
    "Also check the shop domain is the myshopify one (my-store.myshopify.com), not the "
    "admin.shopify.com URL."
)


# Which access scope each Admin API field needs, for the "Access denied for X
# field" errors Shopify returns when an app version is missing one.
SCOPE_FOR_FIELD = {
    "productVariants": "read_products",
    "products": "read_products",
    "customers": "read_customers",
    "customerCreate": "write_customers",
    "customerUpdate": "write_customers",
    "orders": "read_orders",
    "orderCreate": "write_orders",
    "orderUpdate": "write_orders",
    "locations": "read_locations",
    "shop": "read_products",
}


def scope_hint(message: str) -> str:
    """Turn 'Access denied for productVariants field' into something actionable."""
    match = re.search(r"Access denied for (\w+)", message or "")
    if not match:
        return ""
    field = match.group(1)
    scope = SCOPE_FOR_FIELD.get(field)
    needed = f"'{scope}'" if scope else "the matching read/write"
    return (
        f"\nThe app is missing the {needed} access scope. In the Dev Dashboard open the app, "
        "add the scopes under the app version's configuration, RELEASE that version, then mint "
        "a new token — scopes only take effect once the version is released and the token is "
        "re-issued."
    )


def _describe_http_error(status: int, body: str) -> str:
    if status in (401, 403):
        return f"HTTP {status}: {body[:300]}\n{AUTH_HELP}"
    if status == 404:
        return (f"HTTP 404: {body[:300]}\nCheck the shop domain and the API version — "
                "a version Shopify has retired returns 404.")
    return f"HTTP {status}: {body[:800]}"


# Shopify throttles order/customer mutations on a separate bucket from the
# GraphQL query-cost one, and reports it as a userError rather than an HTTP
# 429 — so it never shows up in the cost-based backoff in graphql() below.
THROTTLED_MESSAGE_RE = re.compile(r"too many (attempts|requests)|try again later", re.I)


class UserError(RuntimeError):
    """A `userErrors` payload from a mutation — a data problem, not transport."""

    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors or []
        super().__init__("; ".join(self.format_list()))

    def format_list(self) -> List[str]:
        out = []
        for err in self.errors:
            field = ".".join(err.get("field") or []) or "-"
            out.append(f"{field}: {err.get('message')}")
        return out

    @property
    def messages(self) -> str:
        return "; ".join(self.format_list())

    @property
    def is_throttled(self) -> bool:
        return any(THROTTLED_MESSAGE_RE.search(str(e.get("message") or "")) for e in self.errors)


class ShopifyClient:
    def __init__(
        self,
        cfg: ShopifyConfig,
        log: Optional[Callable[[str, str], None]] = None,
        max_retries: int = 5,
        request_delay: float = 0.0,
        save=None,
    ):
        self.cfg = cfg
        self.max_retries = max_retries
        self.request_delay = request_delay
        self._log = log or (lambda msg, level="info": None)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "woo2shopify/1.0",
            }
        )
        self.tokens = TokenSource(cfg, log=self._log, save=save)
        self._available = 1000.0
        self._restore_rate = 50.0

    def _apply_token(self, force: bool = False) -> None:
        try:
            self.session.headers["X-Shopify-Access-Token"] = self.tokens.token(force=force)
        except OAuthError as exc:
            raise ShopifyError(str(exc)) from exc

    # -------------------------------------------------------------- GraphQL
    def _respect_bucket(self) -> None:
        if self._available < 250:
            wait = max(0.0, (400 - self._available) / max(self._restore_rate, 1.0))
            if wait > 0:
                time.sleep(min(wait, 10.0))

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}})
        delay = 2.0
        refreshed = False
        for attempt in range(1, self.max_retries + 1):
            self._respect_bucket()
            try:
                self._apply_token()
                resp = self.session.post(self.cfg.graphql_url, data=payload, timeout=90)
            except requests.RequestException as exc:
                self._log(f"Shopify network error ({exc}); retry {attempt}/{self.max_retries}", "warn")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After") or delay)
                self._log(f"Shopify HTTP {resp.status_code}; retry in {wait:.0f}s", "warn")
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code == 401 and self.tokens.can_refresh and not refreshed:
                refreshed = True
                self._log("Token rejected — minting a fresh one and retrying.", "warn")
                self._apply_token(force=True)
                continue
            if resp.status_code >= 400:
                raise ShopifyError(_describe_http_error(resp.status_code, resp.text))

            body = resp.json()
            cost = (body.get("extensions") or {}).get("cost") or {}
            throttle = cost.get("throttleStatus") or {}
            if throttle:
                self._available = float(throttle.get("currentlyAvailable", self._available))
                self._restore_rate = float(throttle.get("restoreRate", self._restore_rate))

            errors = body.get("errors")
            if errors:
                codes = {(e.get("extensions") or {}).get("code") for e in errors}
                if "THROTTLED" in codes and attempt < self.max_retries:
                    self._log("Shopify throttled; backing off", "warn")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                joined = "; ".join(e.get("message", "?") for e in errors)
                raise ShopifyError(joined + scope_hint(joined))

            if self.request_delay:
                time.sleep(self.request_delay)
            return body.get("data") or {}

        raise ShopifyError("GraphQL request failed after retries")

    @staticmethod
    def check_user_errors(node: Dict[str, Any], key: str = "userErrors") -> None:
        errors = (node or {}).get(key) or []
        if errors:
            raise UserError(errors)

    def mutate(
        self,
        query: str,
        variables: Dict[str, Any],
        node_key: str,
        errors_key: str = "userErrors",
    ) -> Dict[str, Any]:
        """Run a mutation, retrying it when Shopify throttles order/customer writes.

        This throttle is separate from the GraphQL query-cost bucket graphql()
        already backs off on — it comes back as a userError ("Too many
        attempts. Please try again later."), not an HTTP 429, so it needs its
        own retry loop here rather than being caught upstream.
        """
        delay = 3.0
        for attempt in range(1, self.max_retries + 1):
            data = self.graphql(query, variables)
            node = data.get(node_key) or {}
            try:
                self.check_user_errors(node, errors_key)
            except UserError as exc:
                if exc.is_throttled and attempt < self.max_retries:
                    self._log(
                        f"Shopify is rate-limiting {node_key} — retrying in {delay:.0f}s "
                        f"(attempt {attempt}/{self.max_retries})", "warn",
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise
            return node
        raise AssertionError("unreachable")  # loop above always returns or raises

    # ------------------------------------------------------------------ REST
    def rest(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.cfg.rest_url(path)
        delay = 2.0
        refreshed = False
        for attempt in range(1, self.max_retries + 1):
            try:
                self._apply_token()
                resp = self.session.request(
                    method, url, data=json.dumps(payload) if payload is not None else None,
                    params=params, timeout=90,
                )
            except requests.RequestException as exc:
                self._log(f"Shopify REST network error ({exc}); retry {attempt}/{self.max_retries}", "warn")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After") or delay)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code == 401 and self.tokens.can_refresh and not refreshed:
                refreshed = True
                self._apply_token(force=True)
                continue
            if resp.status_code >= 400:
                raise ShopifyError(f"{method} {path} — " + _describe_http_error(resp.status_code, resp.text))

            limit = resp.headers.get("X-Shopify-Shop-Api-Call-Limit", "")
            if limit and "/" in limit:
                used, cap = (int(x) for x in limit.split("/"))
                if used > cap * 0.8:
                    time.sleep(1.0)
            if self.request_delay:
                time.sleep(self.request_delay)
            return resp.json() if resp.content else {}

        raise ShopifyError(f"REST {method} {path} failed after retries")

    # ------------------------------------------------------------ shop info
    def shop_info(self) -> Dict[str, Any]:
        data = self.graphql(
            "{ shop { name myshopifyDomain email currencyCode ianaTimezone "
            "plan { displayName } } }"
        )
        return data.get("shop") or {}

    def primary_location(self) -> str:
        data = self.graphql(
            "{ locations(first: 5, includeInactive: false) { edges { node { id name isActive } } } }"
        )
        for edge in (data.get("locations") or {}).get("edges", []):
            node = edge["node"]
            if node.get("isActive", True):
                return node["id"]
        return ""

    SCOPES_QUERY = "{ currentAppInstallation { accessScopes { handle } } }"

    def granted_scopes(self) -> List[str]:
        """The scopes this token actually carries, straight from Shopify."""
        data = self.graphql(self.SCOPES_QUERY)
        installation = data.get("currentAppInstallation") or {}
        return [entry["handle"] for entry in (installation.get("accessScopes") or [])]

    # -------------------------------------------------------------- variants
    VARIANTS_QUERY = """
    query($cursor: String) {
      productVariants(first: 250, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges { node { id legacyResourceId sku displayName product { id title } } }
      }
    }
    """

    def iter_variants(self) -> Iterator[Dict[str, Any]]:
        cursor = None
        while True:
            data = self.graphql(self.VARIANTS_QUERY, {"cursor": cursor})
            block = data.get("productVariants") or {}
            for edge in block.get("edges", []):
                node = edge["node"]
                yield {
                    "sku": (node.get("sku") or "").strip(),
                    "variant_gid": node["id"],
                    "variant_id": str(node.get("legacyResourceId") or ""),
                    "product_gid": (node.get("product") or {}).get("id", ""),
                    "title": node.get("displayName") or "",
                }
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return
            cursor = page.get("endCursor")

    # ------------------------------------------------------------- customers
    CUSTOMER_SEARCH = """
    query($q: String!) {
      customers(first: 1, query: $q) {
        edges { node { id legacyResourceId email } }
      }
    }
    """

    def find_customer(self, email: str = "", phone: str = "", woo_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        clauses = []
        if email:
            clauses.append(f'email:"{_escape(email)}"')
        elif phone:
            clauses.append(f'phone:"{_escape(phone)}"')
        elif woo_id:
            clauses.append(f'tag:"woo-customer-{woo_id}"')
        if not clauses:
            return None
        data = self.graphql(self.CUSTOMER_SEARCH, {"q": " AND ".join(clauses)})
        edges = (data.get("customers") or {}).get("edges") or []
        return edges[0]["node"] if edges else None

    CUSTOMER_CREATE = """
    mutation customerCreate($input: CustomerInput!) {
      customerCreate(input: $input) {
        customer { id legacyResourceId email }
        userErrors { field message }
      }
    }
    """

    CUSTOMER_UPDATE = """
    mutation customerUpdate($input: CustomerInput!) {
      customerUpdate(input: $input) {
        customer { id legacyResourceId email }
        userErrors { field message }
      }
    }
    """

    def create_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        node = self.mutate(self.CUSTOMER_CREATE, {"input": payload}, "customerCreate")
        return node.get("customer") or {}

    def update_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        node = self.mutate(self.CUSTOMER_UPDATE, {"input": payload}, "customerUpdate")
        return node.get("customer") or {}

    # ---------------------------------------------------------------- orders
    ORDER_SEARCH = """
    query($q: String!) {
      orders(first: 1, query: $q) {
        edges { node { id legacyResourceId name } }
      }
    }
    """

    def find_order_by_tag(self, tag: str) -> Optional[Dict[str, Any]]:
        data = self.graphql(self.ORDER_SEARCH, {"q": f'tag:"{_escape(tag)}"'})
        edges = (data.get("orders") or {}).get("edges") or []
        return edges[0]["node"] if edges else None

    ORDER_CREATE = """
    mutation orderCreate($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
      orderCreate(order: $order, options: $options) {
        order { id legacyResourceId name }
        userErrors { field message }
      }
    }
    """

    def create_order_graphql(self, order: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
        node = self.mutate(self.ORDER_CREATE, {"order": order, "options": options}, "orderCreate")
        return node.get("order") or {}

    def create_order_rest(self, order: Dict[str, Any]) -> Dict[str, Any]:
        body = self.rest("POST", "orders.json", {"order": order})
        created = body.get("order") or {}
        return {
            "id": f"gid://shopify/Order/{created.get('id')}" if created.get("id") else "",
            "legacyResourceId": str(created.get("id") or ""),
            "name": created.get("name") or "",
        }

    # ----------------------------------------------------------- order notes
    ORDER_UPDATE = """
    mutation orderUpdate($input: OrderInput!) {
      orderUpdate(input: $input) {
        order { id }
        userErrors { field message }
      }
    }
    """

    def set_order_note(self, order_gid: str, note: str) -> None:
        self.mutate(self.ORDER_UPDATE, {"input": {"id": order_gid, "note": note[:5000]}}, "orderUpdate")


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
