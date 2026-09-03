"""Shopify Admin API client (GraphQL first, REST fallback) with leaky-bucket
throttling, retries and the handful of queries/mutations this tool needs.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from .config import ShopifyConfig

RETRY_STATUS = {429, 500, 502, 503, 504}


class ShopifyError(RuntimeError):
    pass


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


class ShopifyClient:
    def __init__(
        self,
        cfg: ShopifyConfig,
        log: Optional[Callable[[str, str], None]] = None,
        max_retries: int = 5,
        request_delay: float = 0.0,
    ):
        self.cfg = cfg
        self.max_retries = max_retries
        self.request_delay = request_delay
        self._log = log or (lambda msg, level="info": None)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Shopify-Access-Token": cfg.access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "woo2shopify/1.0",
            }
        )
        self._available = 1000.0
        self._restore_rate = 50.0

    # -------------------------------------------------------------- GraphQL
    def _respect_bucket(self) -> None:
        if self._available < 250:
            wait = max(0.0, (400 - self._available) / max(self._restore_rate, 1.0))
            if wait > 0:
                time.sleep(min(wait, 10.0))

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}})
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            self._respect_bucket()
            try:
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
            if resp.status_code >= 400:
                raise ShopifyError(f"HTTP {resp.status_code}: {resp.text[:800]}")

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
                raise ShopifyError("; ".join(e.get("message", "?") for e in errors))

            if self.request_delay:
                time.sleep(self.request_delay)
            return body.get("data") or {}

        raise ShopifyError("GraphQL request failed after retries")

    @staticmethod
    def check_user_errors(node: Dict[str, Any], key: str = "userErrors") -> None:
        errors = (node or {}).get(key) or []
        if errors:
            raise UserError(errors)

    # ------------------------------------------------------------------ REST
    def rest(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.cfg.rest_url(path)
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
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
            if resp.status_code >= 400:
                raise ShopifyError(f"HTTP {resp.status_code} {method} {path}: {resp.text[:800]}")

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
        data = self.graphql(self.CUSTOMER_CREATE, {"input": payload})
        node = data.get("customerCreate") or {}
        self.check_user_errors(node)
        return node.get("customer") or {}

    def update_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self.graphql(self.CUSTOMER_UPDATE, {"input": payload})
        node = data.get("customerUpdate") or {}
        self.check_user_errors(node)
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
        data = self.graphql(self.ORDER_CREATE, {"order": order, "options": options})
        node = data.get("orderCreate") or {}
        self.check_user_errors(node)
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
        data = self.graphql(self.ORDER_UPDATE, {"input": {"id": order_gid, "note": note[:5000]}})
        self.check_user_errors(data.get("orderUpdate") or {})


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
