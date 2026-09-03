"""Orchestrates the WooCommerce -> Shopify migration.

The migrator is UI-agnostic: it reports through a Reporter object and checks a
threading.Event pair for pause/stop, so both the CLI and the Qt worker drive it
the same way.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from . import transform
from .config import AppConfig, EXPORT_DIR, STATE_PATH, ensure_dirs
from .shopify_api import ShopifyClient, ShopifyError, UserError
from .state import StateStore
from .woo import WooClient, WooError


@dataclass
class Reporter:
    """Callback bundle. Every hook is optional."""

    on_log: Callable[[str, str], None] = lambda msg, level="info": None
    on_progress: Callable[[str, int, int], None] = lambda phase, done, total: None
    on_stats: Callable[[Dict[str, int]], None] = lambda stats: None
    on_record: Callable[[Dict[str, Any]], None] = lambda row: None

    def log(self, message: str, level: str = "info") -> None:
        self.on_log(message, level)


class Stopped(Exception):
    pass


@dataclass
class Control:
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def checkpoint(self) -> None:
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.2)
        if self.stop_event.is_set():
            raise Stopped()


class Migrator:
    def __init__(
        self,
        config: AppConfig,
        reporter: Optional[Reporter] = None,
        control: Optional[Control] = None,
        state_path=STATE_PATH,
    ):
        ensure_dirs()
        self.cfg = config
        self.opts = config.options
        self.reporter = reporter or Reporter()
        self.control = control or Control()
        self.state = StateStore(state_path)
        self.woo = WooClient(config.woo, log=self.reporter.log, max_retries=self.opts.max_retries)
        self.shopify = ShopifyClient(
            config.shopify,
            log=self.reporter.log,
            max_retries=self.opts.max_retries,
            request_delay=self.opts.request_delay,
        )
        self.location_gid = config.shopify.location_id or ""
        self.stats: Dict[str, int] = {
            "customers_created": 0,
            "customers_existing": 0,
            "customers_failed": 0,
            "customers_skipped": 0,
            "orders_created": 0,
            "orders_existing": 0,
            "orders_failed": 0,
            "orders_skipped": 0,
            "warnings": 0,
        }

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.state.close()

    def _bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount
        self.reporter.on_stats(dict(self.stats))

    # ----------------------------------------------------------- date window
    def date_range(self) -> tuple:
        now = datetime.now(timezone.utc)
        if self.opts.date_from:
            start = _parse_date(self.opts.date_from) or (now - timedelta(days=365 * self.opts.years_back))
        else:
            start = now - timedelta(days=int(365.25 * max(1, self.opts.years_back)))
        end = _parse_date(self.opts.date_to) or now + timedelta(days=1)
        return start, end

    # ------------------------------------------------------------ connection
    def test_woo(self) -> Dict[str, Any]:
        return self.woo.test_connection()

    def test_shopify(self) -> Dict[str, Any]:
        info = self.shopify.shop_info()
        if not self.location_gid:
            self.location_gid = self.shopify.primary_location()
            info["location"] = self.location_gid
        return info

    # -------------------------------------------------------- variant map
    def build_variant_map(self) -> int:
        self.reporter.log("Fetching Shopify product variants…")
        self.state.clear_variants()
        batch: List[Dict[str, str]] = []
        total = 0
        for variant in self.shopify.iter_variants():
            self.control.checkpoint()
            batch.append(variant)
            if len(batch) >= 500:
                total += self._flush_variants(batch)
                batch = []
                self.reporter.on_progress("variants", total, 0)
        if batch:
            total += self._flush_variants(batch)
        self.state.set_meta("variant_map_built_at", datetime.now(timezone.utc).isoformat())
        self.reporter.log(f"Variant map ready: {self.state.counts()['variants']} SKUs indexed.", "success")
        self.reporter.on_progress("variants", total, total)
        return total

    def _flush_variants(self, batch: List[Dict[str, str]]) -> int:
        self.state.save_variants(batch)
        self.state.save_variant_titles(batch)
        return len(batch)

    def _variant_lookup(self, sku: str, title: str) -> Optional[Dict[str, str]]:
        mode = self.opts.match_by
        if mode == "none":
            return None
        if sku:
            row = self.state.variant_by_sku(sku)
            if row:
                return {"variant_gid": row["variant_gid"], "variant_id": row["variant_id"]}
        if mode == "sku_then_title" and title:
            row = self.state.variant_by_title(title)
            if row:
                return {"variant_gid": row["variant_gid"], "variant_id": ""}
        return None

    # -------------------------------------------------------------- customers
    def migrate_customers(self) -> None:
        self.reporter.log("Counting WooCommerce customers…")
        total = self.woo.customer_count()
        self.reporter.log(f"{total} WooCommerce customers found.")
        done = 0
        for customer in self.woo.iter_customers(page_size=self.opts.page_size):
            self.control.checkpoint()
            done += 1
            self._migrate_one_customer(customer)
            self.reporter.on_progress("customers", done, total)

        if self.opts.include_wp_users:
            self.migrate_wp_users()

    def migrate_wp_users(self) -> None:
        if not (self.cfg.woo.wp_username and self.cfg.woo.wp_app_password):
            self.reporter.log("Skipping WordPress users: no application password configured.", "warn")
            return
        self.reporter.log("Importing WordPress users (non-customer roles included)…")
        done = 0
        for user in self.woo.iter_wp_users(page_size=self.opts.page_size):
            self.control.checkpoint()
            done += 1
            woo_id = int(user.get("id") or 0)
            key = -woo_id  # negative keys keep WP users apart from Woo customers
            email = (user.get("email") or "").strip().lower()
            if not email:
                self.state.record_customer(key, "", "wp_user", "skipped", error="no email")
                self._bump("customers_skipped")
                continue
            if self.opts.resume:
                row = self.state.customer_status(key)
                if row and row["status"] == "done":
                    continue
            payload = transform.customer_from_wp_user(user, self.opts)
            self._push_customer(key, email, "wp_user", payload, label=f"WP user #{woo_id}")
            self.reporter.on_progress("customers", done, 0)

    def _migrate_one_customer(self, customer: Dict[str, Any]) -> None:
        woo_id = int(customer.get("id") or 0)
        email = (customer.get("email") or "").strip().lower()

        if self.opts.resume:
            row = self.state.customer_status(woo_id)
            if row and row["status"] == "done":
                return
        if not email:
            self.state.record_customer(woo_id, "", "woo_customer", "skipped", error="no email address")
            self._bump("customers_skipped")
            return

        payload = transform.customer_from_woo(customer, self.opts)
        self._push_customer(woo_id, email, "woo_customer", payload, label=f"customer #{woo_id} {email}")

    def _push_customer(self, key: int, email: str, source: str, payload: Dict[str, Any], label: str) -> Optional[str]:
        if self.opts.dry_run:
            self.reporter.log(f"[dry-run] would create {label}")
            self.state.record_customer(key, email, source, "skipped", error="dry-run")
            self._bump("customers_skipped")
            return None

        cached = self.state.customer_gid_by_email(email)
        if cached:
            self.state.record_customer(key, email, source, "done", cached, "")
            self._bump("customers_existing")
            return cached

        try:
            customer = self.shopify.create_customer(payload)
        except UserError as exc:
            message = exc.messages
            if "taken" in message.lower() or "already" in message.lower():
                existing = self._find_existing_customer(email)
                if existing:
                    gid = existing["id"]
                    self.state.record_customer(key, email, source, "done", gid, str(existing.get("legacyResourceId") or ""))
                    self._bump("customers_existing")
                    return gid
            retry_payload = self._strip_problem_fields(payload, message)
            if retry_payload is not None:
                try:
                    customer = self.shopify.create_customer(retry_payload)
                except (UserError, ShopifyError) as exc2:
                    return self._customer_failed(key, email, source, label, str(exc2))
            else:
                return self._customer_failed(key, email, source, label, message)
        except ShopifyError as exc:
            return self._customer_failed(key, email, source, label, str(exc))

        gid = customer.get("id", "")
        legacy = str(customer.get("legacyResourceId") or "")
        self.state.record_customer(key, email, source, "done", gid, legacy)
        self._bump("customers_created")
        return gid

    def _customer_failed(self, key: int, email: str, source: str, label: str, message: str) -> Optional[str]:
        self.reporter.log(f"Customer failed — {label}: {message}", "error")
        self.state.record_customer(key, email, source, "failed", error=message[:1000])
        self._bump("customers_failed")
        self.reporter.on_record({"type": "customer", "woo_id": key, "ref": email, "error": message[:400]})
        return None

    @staticmethod
    def _strip_problem_fields(payload: Dict[str, Any], message: str) -> Optional[Dict[str, Any]]:
        """Retry once without whichever optional field Shopify rejected."""
        lowered = message.lower()
        retry = dict(payload)
        changed = False
        if "phone" in lowered and ("phone" in retry or "addresses" in retry):
            retry.pop("phone", None)
            if "addresses" in retry:
                retry["addresses"] = [{k: v for k, v in a.items() if k != "phone"} for a in retry["addresses"]]
            changed = True
        if "province" in lowered or "zip" in lowered or "country" in lowered or "address" in lowered:
            retry.pop("addresses", None)
            changed = True
        if "metafield" in lowered:
            retry.pop("metafields", None)
            changed = True
        return retry if changed else None

    def _find_existing_customer(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            found = self.shopify.find_customer(email=email)
        except ShopifyError:
            return None
        if found:
            self.state.remember_email(email, found["id"], str(found.get("legacyResourceId") or ""))
        return found

    # ----------------------------------------------------------------- orders
    def migrate_orders(self) -> None:
        start, end = self.date_range()
        self.reporter.log(
            f"Importing orders between {start:%Y-%m-%d} and {end:%Y-%m-%d} "
            f"(statuses: {', '.join(self.opts.order_statuses) or 'any'})"
        )
        try:
            total = self.woo.order_count(start, end, self.opts.order_statuses)
        except WooError:
            total = 0
        self.reporter.log(f"{total or 'unknown number of'} orders in range.")

        done = 0
        for order in self.woo.iter_orders(
            start, end, self.opts.order_statuses,
            page_size=self.opts.page_size, window_days=self.opts.window_days,
        ):
            self.control.checkpoint()
            done += 1
            self._migrate_one_order(order)
            self.reporter.on_progress("orders", done, total)

    def _migrate_one_order(self, order: Dict[str, Any]) -> None:
        woo_id = int(order.get("id") or 0)
        number = str(order.get("number") or woo_id)
        email = (order.get("billing") or {}).get("email", "")
        total = str(order.get("total") or "")
        created = str(order.get("date_created_gmt") or order.get("date_created") or "")

        if self.opts.resume:
            row = self.state.order_status(woo_id)
            if row and row["status"] == "done":
                return
        if self.opts.skip_zero_total and transform.dec(total) == 0:
            self.state.record_order(woo_id, "skipped", number, email, total, created, error="zero total")
            self._bump("orders_skipped")
            return

        # Refunds feed the financial status. The order payload usually carries
        # them already; only fall back to the sub-resource when it does not.
        order.setdefault("refunds", [])
        if not order["refunds"] and (order.get("status") or "").lower() == "refunded":
            try:
                order["refunds"] = self.woo.order_refunds(woo_id)
            except WooError:
                order["refunds"] = []

        customer_gid = ""
        customer_legacy = ""
        if email:
            customer_gid = self.state.customer_gid_by_email(email) or ""
            if not customer_gid:
                found = self._find_existing_customer(email)
                if found:
                    customer_gid = found["id"]
                    customer_legacy = str(found.get("legacyResourceId") or "")
                elif self.opts.include_guest_customers and not self.opts.dry_run:
                    payload = transform.customer_from_guest_order(order, self.opts)
                    customer_gid = self._push_customer(
                        -1000000 - woo_id, email, "guest_order", payload, label=f"guest {email}"
                    ) or ""
            if customer_gid and not customer_legacy:
                customer_legacy = customer_gid.rsplit("/", 1)[-1]

        if self.opts.dry_run:
            payload, _opts, warnings = transform.order_to_graphql(
                order, self.opts, self._variant_lookup, customer_gid, self.location_gid
            )
            for warning in warnings:
                self.reporter.log(f"Order #{number}: {warning}", "warn")
                self._bump("warnings")
            self.reporter.log(f"[dry-run] would import order #{number} ({total} {order.get('currency')}), "
                              f"{len(payload.get('lineItems', []))} line(s)")
            self.state.record_order(woo_id, "skipped", number, email, total, created, error="dry-run")
            self._bump("orders_skipped")
            return

        existing = None
        try:
            existing = self.shopify.find_order_by_tag(transform.woo_order_tag(woo_id))
        except ShopifyError:
            existing = None
        if existing:
            self.state.record_order(
                woo_id, "done", number, email, total, created,
                existing["id"], str(existing.get("legacyResourceId") or ""), existing.get("name", ""),
            )
            self._bump("orders_existing")
            return

        try:
            if self.cfg.shopify.order_api == "rest":
                payload, warnings = transform.order_to_rest(order, self.opts, self._variant_lookup, customer_legacy)
                created_order = self.shopify.create_order_rest(payload)
            else:
                payload, api_options, warnings = transform.order_to_graphql(
                    order, self.opts, self._variant_lookup, customer_gid, self.location_gid
                )
                created_order = self.shopify.create_order_graphql(payload, api_options)
        except (UserError, ShopifyError) as exc:
            message = str(exc)
            self.reporter.log(f"Order #{number} failed: {message}", "error")
            self.state.record_order(woo_id, "failed", number, email, total, created, error=message[:1000])
            self._bump("orders_failed")
            self.reporter.on_record({"type": "order", "woo_id": woo_id, "ref": number, "error": message[:400]})
            return

        for warning in warnings:
            self.reporter.log(f"Order #{number}: {warning}", "warn")
            self._bump("warnings")

        gid = created_order.get("id", "")
        self.state.record_order(
            woo_id, "done", number, email, total, created,
            gid, str(created_order.get("legacyResourceId") or ""), created_order.get("name", ""),
        )
        self._bump("orders_created")

        if self.opts.import_order_notes and gid:
            self._attach_notes(woo_id, number, gid, order)

    def _attach_notes(self, woo_id: int, number: str, gid: str, order: Dict[str, Any]) -> None:
        try:
            notes = self.woo.order_notes(woo_id)
        except WooError:
            return
        text = transform.order_notes_text(notes)
        if not text:
            return
        header = f"WooCommerce order #{number} notes:\n"
        existing_note = (order.get("customer_note") or "").strip()
        body = (existing_note + "\n\n" if existing_note else "") + header + text
        try:
            self.shopify.set_order_note(gid, body)
        except (UserError, ShopifyError) as exc:
            self.reporter.log(f"Order #{number}: could not attach notes ({exc})", "warn")

    # -------------------------------------------------------------- full run
    def run(self) -> Dict[str, int]:
        started = time.time()
        try:
            info = self.test_shopify()
            self.reporter.log(
                f"Connected to Shopify: {info.get('name')} ({info.get('myshopifyDomain')}), "
                f"currency {info.get('currencyCode')}", "success",
            )
            if self.opts.match_variants and self.opts.match_by != "none":
                if not self.state.counts()["variants"]:
                    self.build_variant_map()
                else:
                    self.reporter.log(
                        f"Reusing cached variant map ({self.state.counts()['variants']} SKUs). "
                        "Rebuild it from the Products tab if the catalogue changed."
                    )
            if self.opts.migrate_customers:
                self.migrate_customers()
            if self.opts.migrate_orders:
                self.migrate_orders()
            self.reporter.log(f"Finished in {time.time() - started:.0f}s.", "success")
        except Stopped:
            self.reporter.log("Stopped by user. Re-running will resume where it left off.", "warn")
        finally:
            self.reporter.on_stats(dict(self.stats))
        return dict(self.stats)

    # --------------------------------------------------------------- reports
    def export_reports(self, directory=EXPORT_DIR) -> List[str]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        paths = []
        for table in ("customers", "orders", "variants"):
            path = self.state.dump_csv(table, directory / f"{table}-{stamp}.csv")
            paths.append(str(path))
        return paths


def _parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
