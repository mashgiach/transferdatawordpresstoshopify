"""A tiny stand-in for the WooCommerce and Shopify APIs, used by the tests."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

CUSTOMERS = [
    {
        "id": 11, "email": "Dana@example.com", "first_name": "Dana", "last_name": "Levi",
        "username": "dana", "role": "customer", "date_created_gmt": "2020-02-03T09:00:00",
        "is_paying_customer": True,
        "billing": {"first_name": "Dana", "last_name": "Levi", "company": "Acme",
                    "address_1": "1 Herzl St", "address_2": "Apt 4", "city": "Tel Aviv",
                    "state": "", "postcode": "6100000", "country": "IL",
                    "email": "dana@example.com", "phone": "+972541234567"},
        "shipping": {"first_name": "Dana", "last_name": "Levi", "address_1": "1 Herzl St",
                     "city": "Tel Aviv", "state": "", "postcode": "6100000", "country": "IL"},
        "meta_data": [],
    },
    {
        "id": 12, "email": "sam@example.com", "first_name": "Sam", "last_name": "Cohen",
        "username": "sam", "role": "customer", "date_created_gmt": "2021-06-01T12:00:00",
        "billing": {"first_name": "Sam", "last_name": "Cohen", "address_1": "22 Main",
                    "city": "Austin", "state": "TX", "postcode": "73301", "country": "US",
                    "email": "sam@example.com", "phone": "512-555-0100"},
        "shipping": {}, "meta_data": [],
    },
    {  # no email at all -> must be skipped, not crash
        "id": 13, "email": "", "first_name": "Ghost", "last_name": "User",
        "username": "ghost", "role": "customer", "date_created_gmt": "2022-01-01T00:00:00",
        "billing": {}, "shipping": {}, "meta_data": [],
    },
]

ORDERS = [
    {
        "id": 501, "number": "501", "status": "completed", "currency": "ILS",
        "date_created_gmt": "2022-03-04T10:15:00", "date_paid_gmt": "2022-03-04T10:16:00",
        "date_modified_gmt": "2022-03-05T08:00:00",
        "total": "234.00", "discount_total": "10.00", "prices_include_tax": True,
        "payment_method": "paypal", "payment_method_title": "PayPal", "order_key": "wc_order_abc",
        "customer_id": 11, "customer_note": "Please ring the bell",
        "billing": CUSTOMERS[0]["billing"], "shipping": CUSTOMERS[0]["shipping"],
        "line_items": [
            {"id": 1, "name": "Blue Mug", "sku": "MUG-BLUE", "quantity": 2,
             "subtotal": "200.00", "subtotal_tax": "34.00", "total": "190.00", "total_tax": "32.30",
             "price": 95.0, "meta_data": [{"key": "Engraving", "display_key": "Engraving",
                                           "value": "Yes", "display_value": "Yes"}]},
            {"id": 2, "name": "Vanished Product", "sku": "GONE-1", "quantity": 1,
             "subtotal": "30.00", "subtotal_tax": "0", "total": "30.00", "total_tax": "0",
             "price": 30.0, "meta_data": []},
        ],
        "shipping_lines": [{"method_title": "Flat rate", "method_id": "flat_rate",
                            "total": "20.00", "total_tax": "0"}],
        "tax_lines": [{"label": "VAT", "rate_percent": 17, "tax_total": "32.30",
                       "shipping_tax_total": "0", "rate_code": "IL-VAT"}],
        "fee_lines": [{"name": "Gift wrap", "total": "4.00"}],
        "coupon_lines": [{"code": "SPRING10"}],
        "refunds": [],
    },
    {
        "id": 502, "number": "502", "status": "refunded", "currency": "ILS",
        "date_created_gmt": "2023-07-19T14:00:00", "date_paid_gmt": "2023-07-19T14:01:00",
        "total": "90.00", "discount_total": "0", "prices_include_tax": False,
        "payment_method": "stripe", "payment_method_title": "Credit card", "order_key": "wc_order_def",
        "customer_id": 12, "customer_note": "",
        "billing": CUSTOMERS[1]["billing"], "shipping": {},
        "line_items": [{"id": 3, "name": "Red Mug", "sku": "MUG-RED", "quantity": 1,
                        "subtotal": "90.00", "subtotal_tax": "0", "total": "90.00",
                        "total_tax": "0", "price": 90.0, "meta_data": []}],
        "shipping_lines": [], "tax_lines": [], "fee_lines": [], "coupon_lines": [],
        "refunds": [{"id": 9, "total": "-90.00", "reason": "returned"}],
    },
    {   # guest order — no registered customer behind it
        "id": 503, "number": "503", "status": "processing", "currency": "ILS",
        "date_created_gmt": "2024-11-02T08:30:00", "date_paid_gmt": "2024-11-02T08:31:00",
        "total": "45.00", "discount_total": "0", "prices_include_tax": False,
        "payment_method": "cod", "payment_method_title": "Cash on delivery", "order_key": "wc_order_ghi",
        "customer_id": 0, "customer_note": "",
        "billing": {"first_name": "Guest", "last_name": "Buyer", "address_1": "9 Ben Gurion",
                    "city": "Haifa", "state": "", "postcode": "3100000", "country": "IL",
                    "email": "guest@example.com", "phone": "0501234567"},
        "shipping": {},
        "line_items": [{"id": 4, "name": "Sticker pack", "sku": "", "quantity": 3,
                        "subtotal": "45.00", "subtotal_tax": "0", "total": "45.00",
                        "total_tax": "0", "price": 15.0, "meta_data": []}],
        "shipping_lines": [], "tax_lines": [], "fee_lines": [], "coupon_lines": [], "refunds": [],
    },
    {   # cancelled, empty line items -> placeholder path
        "id": 504, "number": "504", "status": "cancelled", "currency": "ILS",
        "date_created_gmt": "2025-01-15T09:00:00", "date_modified_gmt": "2025-01-16T09:00:00",
        "total": "0.00", "discount_total": "0", "prices_include_tax": False,
        "payment_method": "", "payment_method_title": "", "order_key": "wc_order_jkl",
        "customer_id": 11, "customer_note": "",
        "billing": CUSTOMERS[0]["billing"], "shipping": {},
        "line_items": [], "shipping_lines": [], "tax_lines": [], "fee_lines": [],
        "coupon_lines": [], "refunds": [],
    },
]

VARIANTS = [
    {"id": "gid://shopify/ProductVariant/1001", "legacyResourceId": "1001", "sku": "MUG-BLUE",
     "displayName": "Blue Mug - Default", "product": {"id": "gid://shopify/Product/1", "title": "Blue Mug"}},
    {"id": "gid://shopify/ProductVariant/1002", "legacyResourceId": "1002", "sku": "MUG-RED",
     "displayName": "Red Mug - Default", "product": {"id": "gid://shopify/Product/2", "title": "Red Mug"}},
]


class Recorder:
    def __init__(self):
        self.customers = []
        self.orders = []
        self.notes = []
        self.existing_emails = {}


class Handler(BaseHTTPRequestHandler):
    recorder = Recorder()

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ WooCommerce
    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        page = int(query.get("page", 1))

        if url.path.endswith("/wp-json/wc/v3/customers"):
            items = CUSTOMERS if page == 1 else []
            return self._send(items, headers={"X-WP-Total": str(len(CUSTOMERS)), "X-WP-TotalPages": "1"})

        if url.path.endswith("/wp-json/wc/v3/orders"):
            after = query.get("after", "")
            before = query.get("before", "")
            selected = [o for o in ORDERS if (not after or o["date_created_gmt"] >= after[:19])
                        and (not before or o["date_created_gmt"] <= before[:19])]
            if int(query.get("per_page", 100)) == 1 and page == 1 and "status" in query and not after:
                return self._send(selected[:1], headers={"X-WP-Total": str(len(selected)), "X-WP-TotalPages": "1"})
            items = selected if page == 1 else []
            return self._send(items, headers={"X-WP-Total": str(len(selected)), "X-WP-TotalPages": "1"})

        match = re.search(r"/orders/(\d+)/notes", url.path)
        if match:
            return self._send([{"id": 1, "author": "system", "customer_note": False,
                                "date_created_gmt": "2022-03-04T11:00:00",
                                "note": f"Imported note for order {match.group(1)}"}])
        match = re.search(r"/orders/(\d+)/refunds", url.path)
        if match:
            return self._send([])
        if url.path.endswith("/wp-json/wp/v2/users"):
            items = [{"id": 77, "name": "Staff Member", "slug": "staff",
                      "email": "staff@example.com", "roles": ["editor"]}] if page == 1 else []
            return self._send(items, headers={"X-WP-TotalPages": "1"})

        return self._send({"message": "not found"}, 404)

    # ---------------------------------------------------------------- Shopify
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else "{}"
        payload = json.loads(raw) if raw else {}
        url = urlparse(self.path)

        if url.path.endswith("/graphql.json"):
            return self._graphql(payload)
        if url.path.endswith("/orders.json"):
            order = payload["order"]
            self.recorder.orders.append(order)
            oid = 8000 + len(self.recorder.orders)
            return self._send({"order": {"id": oid, "name": f"#{oid}"}})
        return self._send({"errors": "unknown"}, 404)

    def _graphql(self, payload):
        query = payload.get("query", "")
        variables = payload.get("variables") or {}
        cost = {"cost": {"throttleStatus": {"currentlyAvailable": 900, "restoreRate": 50, "maximumAvailable": 1000}}}

        if "shop {" in query:
            return self._send({"data": {"shop": {"name": "Test Store", "myshopifyDomain": "test.myshopify.com",
                                                 "email": "a@b.c", "currencyCode": "ILS",
                                                 "ianaTimezone": "Asia/Jerusalem",
                                                 "plan": {"displayName": "Basic"}}}, "extensions": cost})
        if "locations(" in query:
            return self._send({"data": {"locations": {"edges": [
                {"node": {"id": "gid://shopify/Location/55", "name": "Main", "isActive": True}}]}},
                "extensions": cost})
        if "productVariants(" in query:
            return self._send({"data": {"productVariants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "edges": [{"node": v} for v in VARIANTS]}}, "extensions": cost})
        if "customerCreate(" in query:
            data = variables["input"]
            email = (data.get("email") or "").lower()
            if email in self.recorder.existing_emails:
                return self._send({"data": {"customerCreate": {"customer": None, "userErrors": [
                    {"field": ["email"], "message": "Email has already been taken"}]}}, "extensions": cost})
            if data.get("phone") and not data["phone"].startswith("+"):
                return self._send({"data": {"customerCreate": {"customer": None, "userErrors": [
                    {"field": ["phone"], "message": "Phone is invalid"}]}}, "extensions": cost})
            self.recorder.customers.append(data)
            cid = 7000 + len(self.recorder.customers)
            gid = f"gid://shopify/Customer/{cid}"
            self.recorder.existing_emails[email] = gid
            return self._send({"data": {"customerCreate": {
                "customer": {"id": gid, "legacyResourceId": str(cid), "email": email},
                "userErrors": []}}, "extensions": cost})
        if "customers(first: 1" in query:
            match = re.search(r'email:"([^"]+)"', variables.get("q", ""))
            email = (match.group(1) if match else "").lower()
            gid = self.recorder.existing_emails.get(email)
            edges = [{"node": {"id": gid, "legacyResourceId": gid.rsplit("/", 1)[-1], "email": email}}] if gid else []
            return self._send({"data": {"customers": {"edges": edges}}, "extensions": cost})
        if "orders(first: 1" in query:
            tag = re.search(r'tag:"([^"]+)"', variables.get("q", ""))
            wanted = tag.group(1) if tag else ""
            for order in self.recorder.orders:
                if wanted in (order.get("tags") or []):
                    return self._send({"data": {"orders": {"edges": [
                        {"node": {"id": order["_gid"], "legacyResourceId": order["_id"],
                                  "name": order["_name"]}}]}}, "extensions": cost})
            return self._send({"data": {"orders": {"edges": []}}, "extensions": cost})
        if "orderCreate(" in query:
            order = dict(variables["order"])
            oid = 9000 + len(self.recorder.orders)
            order["_id"] = str(oid)
            order["_gid"] = f"gid://shopify/Order/{oid}"
            order["_name"] = f"#{oid}"
            self.recorder.orders.append(order)
            return self._send({"data": {"orderCreate": {
                "order": {"id": order["_gid"], "legacyResourceId": order["_id"], "name": order["_name"]},
                "userErrors": []}}, "extensions": cost})
        if "orderUpdate(" in query:
            self.recorder.notes.append(variables["input"])
            return self._send({"data": {"orderUpdate": {"order": {"id": variables["input"]["id"]},
                                                        "userErrors": []}}, "extensions": cost})
        return self._send({"errors": [{"message": f"unhandled query: {query[:80]}"}]})


def start_server():
    Handler.recorder = Recorder()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, Handler.recorder
