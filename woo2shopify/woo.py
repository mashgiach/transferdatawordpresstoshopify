"""WooCommerce REST v3 + WordPress REST client with retries and date windowing."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from .config import WooConfig

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class WooError(RuntimeError):
    pass


class WooClient:
    def __init__(self, cfg: WooConfig, log: Optional[Callable[[str, str], None]] = None, max_retries: int = 5):
        self.cfg = cfg
        self.max_retries = max_retries
        self._log = log or (lambda msg, level="info": None)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "woo2shopify/1.0", "Accept": "application/json"})

    # ------------------------------------------------------------------ core
    def _auth(self) -> Optional[HTTPBasicAuth]:
        if self.cfg.query_string_auth:
            return None
        return HTTPBasicAuth(self.cfg.consumer_key, self.cfg.consumer_secret)

    def _request(self, url: str, params: Dict[str, Any], auth=None, basic=None) -> requests.Response:
        params = dict(params or {})
        if auth is None and basic is None and self.cfg.query_string_auth:
            params["consumer_key"] = self.cfg.consumer_key
            params["consumer_secret"] = self.cfg.consumer_secret

        delay = 2.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    auth=basic if basic is not None else (auth if auth is not None else self._auth()),
                    timeout=self.cfg.timeout,
                    verify=self.cfg.verify_ssl,
                )
            except requests.RequestException as exc:
                last_exc = exc
                self._log(f"Woo network error ({exc}); retry {attempt}/{self.max_retries}", "warn")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After") or delay)
                self._log(f"Woo HTTP {resp.status_code}; retry in {wait:.0f}s", "warn")
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue

            if resp.status_code >= 400:
                raise WooError(f"{resp.status_code} {resp.request.method} {resp.url}: {resp.text[:500]}")
            return resp

        raise WooError(f"Giving up on {url}: {last_exc}")

    def get(self, path: str, **params) -> Any:
        return self._request(f"{self.cfg.api_root}/{path.lstrip('/')}", params).json()

    # ------------------------------------------------------------ pagination
    def paginate(self, path: str, page_size: int = 100, **params) -> Iterator[Dict[str, Any]]:
        """Page through a Woo collection endpoint."""
        page = 1
        while True:
            query = dict(params)
            query.update({"per_page": min(page_size, 100), "page": page})
            resp = self._request(f"{self.cfg.api_root}/{path.lstrip('/')}", query)
            batch = resp.json()
            if not isinstance(batch, list):
                raise WooError(f"Unexpected payload from {path}: {str(batch)[:300]}")
            for item in batch:
                yield item
            total_pages = int(resp.headers.get("X-WP-TotalPages") or 0)
            if not batch or (total_pages and page >= total_pages) or len(batch) < min(page_size, 100):
                return
            page += 1

    def count(self, path: str, **params) -> int:
        query = dict(params)
        query.update({"per_page": 1, "page": 1})
        resp = self._request(f"{self.cfg.api_root}/{path.lstrip('/')}", query)
        return int(resp.headers.get("X-WP-Total") or 0)

    # ------------------------------------------------------------------ ping
    def test_connection(self) -> Dict[str, Any]:
        total_orders = self.count("orders", status="any")
        total_customers = self.count("customers", role="all")
        return {"orders": total_orders, "customers": total_customers}

    def order_status_report(self) -> List[Dict[str, Any]]:
        """Every order status this store actually has, with how many orders

        carry it — including any custom status from a plugin, which a fixed
        checkbox list could never show. `[{"slug", "name", "total"}, ...]`.
        """
        return self.get("reports/orders/totals") or []

    # ------------------------------------------------------------- customers
    def iter_customers(self, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        yield from self.paginate(
            "customers", page_size=page_size, role="all", orderby="id", order="asc", context="edit"
        )

    def customer_count(self) -> int:
        return self.count("customers", role="all")

    # ---------------------------------------------------------------- orders
    def iter_orders(
        self,
        date_from: datetime,
        date_to: datetime,
        statuses: List[str],
        page_size: int = 100,
        window_days: int = 31,
    ) -> Iterator[Dict[str, Any]]:
        """Yield orders in ascending date order, walking fixed date windows.

        Woo's offset pagination degrades badly past a few thousand rows, so the
        range is sliced into windows small enough that each one pages cleanly.
        """
        status = ",".join(statuses) if statuses else "any"
        seen = set()
        for start, end in _windows(date_from, date_to, window_days):
            params = {
                "status": status,
                "orderby": "date",
                "order": "asc",
                "dates_are_gmt": "true",
                "after": _iso(start),
                "before": _iso(end),
            }
            for order in self.paginate("orders", page_size=page_size, **params):
                oid = order.get("id")
                if oid in seen:
                    continue
                seen.add(oid)
                yield order

    def order_count(self, date_from: datetime, date_to: datetime, statuses: List[str]) -> int:
        status = ",".join(statuses) if statuses else "any"
        return self.count(
            "orders",
            status=status,
            dates_are_gmt="true",
            after=_iso(date_from),
            before=_iso(date_to),
        )

    def order_notes(self, order_id: int) -> List[Dict[str, Any]]:
        try:
            return self.get(f"orders/{order_id}/notes", per_page=100)
        except WooError:
            return []

    def order_refunds(self, order_id: int) -> List[Dict[str, Any]]:
        try:
            return self.get(f"orders/{order_id}/refunds", per_page=100)
        except WooError:
            return []

    # ------------------------------------------------------------- wp users
    def iter_wp_users(self, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """All WordPress users (needs an application password)."""
        if not (self.cfg.wp_username and self.cfg.wp_app_password):
            return
        basic = HTTPBasicAuth(self.cfg.wp_username, self.cfg.wp_app_password)
        page = 1
        while True:
            resp = self._request(
                f"{self.cfg.wp_api_root}/users",
                {"per_page": min(page_size, 100), "page": page, "context": "edit", "orderby": "id", "order": "asc"},
                basic=basic,
            )
            batch = resp.json()
            if not batch:
                return
            for item in batch:
                yield item
            total_pages = int(resp.headers.get("X-WP-TotalPages") or 0)
            if total_pages and page >= total_pages:
                return
            page += 1


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _windows(start: datetime, end: datetime, days: int) -> Iterator[Tuple[datetime, datetime]]:
    days = max(1, int(days))
    cursor = start
    step = timedelta(days=days)
    while cursor < end:
        nxt = min(cursor + step, end)
        # `after`/`before` are exclusive; nudge the lower bound back one second
        # so nothing falls between two adjacent windows.
        yield cursor - timedelta(seconds=1), nxt + timedelta(seconds=1)
        cursor = nxt
