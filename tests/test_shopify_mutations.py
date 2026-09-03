"""Order/customer mutation retries and the tax-line payload shape.

Shopify throttles order and customer mutations on a bucket separate from the
GraphQL query-cost one, and reports it through userErrors rather than an
HTTP 429 — "-: Too many attempts. Please try again later." These tests pin
that retry path, plus the "tax lines on both order and line item" rejection.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from woo2shopify.config import MigrationOptions, ShopifyConfig  # noqa: E402
from woo2shopify.shopify_api import ShopifyClient, UserError  # noqa: E402
from woo2shopify import transform  # noqa: E402

THROTTLED = {"customerCreate": {"customer": None, "userErrors": [
    {"field": [], "message": "Too many attempts. Please try again later."}
]}}
OK_CUSTOMER = {"customerCreate": {"customer": {"id": "gid://shopify/Customer/1", "legacyResourceId": "1"},
                                  "userErrors": []}}
REAL_ERROR = {"customerCreate": {"customer": None, "userErrors": [
    {"field": ["email"], "message": "Email has already been taken"}
]}}


def client():
    return ShopifyClient(ShopifyConfig(shop_domain="t.myshopify.com", access_token="shpat_x"), max_retries=4)


class OrderMutationThrottleTest(unittest.TestCase):
    def test_throttled_user_error_is_retried_and_then_succeeds(self):
        c = client()
        with mock.patch.object(c, "graphql", side_effect=[THROTTLED, THROTTLED, OK_CUSTOMER]) as g, \
             mock.patch("time.sleep"):
            result = c.create_customer({"email": "a@b.com"})
        self.assertEqual(result["id"], "gid://shopify/Customer/1")
        self.assertEqual(g.call_count, 3)

    def test_real_user_error_is_not_retried(self):
        c = client()
        with mock.patch.object(c, "graphql", return_value=REAL_ERROR) as g:
            with self.assertRaises(UserError) as ctx:
                c.create_customer({"email": "dup@b.com"})
        self.assertEqual(g.call_count, 1, "a real data error must fail immediately, not retry")
        self.assertIn("already been taken", str(ctx.exception))

    def test_gives_up_after_max_retries_with_the_real_message(self):
        c = client()
        with mock.patch.object(c, "graphql", return_value=THROTTLED) as g, mock.patch("time.sleep"):
            with self.assertRaises(UserError) as ctx:
                c.create_customer({"email": "a@b.com"})
        self.assertEqual(g.call_count, c.max_retries)
        self.assertIn("Too many attempts", str(ctx.exception))

    def test_order_create_uses_the_same_retry_path(self):
        c = client()
        throttled_order = {"orderCreate": {"order": None, "userErrors": [
            {"field": [], "message": "Too many attempts. Please try again later."}
        ]}}
        ok_order = {"orderCreate": {"order": {"id": "gid://shopify/Order/9", "name": "#9"},
                                    "userErrors": []}}
        with mock.patch.object(c, "graphql", side_effect=[throttled_order, ok_order]), mock.patch("time.sleep"):
            result = c.create_order_graphql({}, {})
        self.assertEqual(result["name"], "#9")

    def test_is_throttled_matches_the_observed_message_only(self):
        self.assertTrue(UserError([{"field": [], "message": "Too many attempts. Please try again later."}]).is_throttled)
        self.assertFalse(UserError([{"field": ["email"], "message": "Email has already been taken"}]).is_throttled)


class MutationPacingTest(unittest.TestCase):
    """After being throttled, subsequent writes should slow down proactively,
    not just react to the same wall again a few orders later."""

    def test_pace_increases_on_a_throttle_and_is_applied_next_time(self):
        c = client()
        with mock.patch.object(c, "graphql", side_effect=[THROTTLED, OK_CUSTOMER]), mock.patch("time.sleep"):
            c.create_customer({"email": "a@b.com"})
        self.assertGreater(c._mutation_pace, 0)

        pace_after_throttle = c._mutation_pace
        with mock.patch.object(c, "graphql", return_value=OK_CUSTOMER) as g, mock.patch("time.sleep") as sleep2:
            c.create_customer({"email": "b@c.com"})
        sleep2.assert_called_once_with(pace_after_throttle)
        self.assertEqual(g.call_count, 1, "a paced-but-otherwise-clean call must not itself retry")

    def test_pace_does_not_grow_past_the_cap(self):
        c = client()
        c._mutation_pace = 8.0
        with mock.patch.object(c, "graphql", side_effect=[THROTTLED, OK_CUSTOMER]), mock.patch("time.sleep"):
            c.create_customer({"email": "a@b.com"})
        self.assertEqual(c._mutation_pace, 8.0)

    def test_pace_eases_off_after_a_run_of_clean_mutations(self):
        c = client()
        c._mutation_pace = 2.0
        with mock.patch.object(c, "graphql", return_value=OK_CUSTOMER), mock.patch("time.sleep"):
            for _ in range(25):
                c.create_customer({"email": "ok@b.com"})
        self.assertLess(c._mutation_pace, 2.0)

    def test_clean_mutation_run_is_not_reset_by_pacing_alone(self):
        c = client()
        with mock.patch.object(c, "graphql", return_value=OK_CUSTOMER), mock.patch("time.sleep"):
            c.create_customer({"email": "a@b.com"})
        self.assertEqual(c._mutation_pace, 0.0, "no throttle ever happened, so there is nothing to pace")


class TaxLinePlacementTest(unittest.TestCase):
    """Shopify: 'Order Tax lines must be associated with either order or line item but not both.'"""

    def _order(self, **overrides):
        order = {
            "id": 19261, "number": "19261", "status": "completed", "currency": "ILS",
            "date_created_gmt": "2024-01-01T00:00:00", "discount_total": "0",
            "prices_include_tax": True, "billing": {}, "shipping": {},
            "line_items": [{
                "id": 1, "name": "Widget", "sku": "W-1", "quantity": 1,
                "subtotal": "100.00", "subtotal_tax": "17.00", "total": "100.00",
                "total_tax": "17.00", "price": 100.0, "meta_data": [],
            }],
            "shipping_lines": [],
            "tax_lines": [{"label": "VAT", "rate_percent": 17, "tax_total": "17.00",
                          "shipping_tax_total": "0", "rate_code": "IL-VAT"}],
            "fee_lines": [], "coupon_lines": [], "refunds": [],
        }
        order.update(overrides)
        return order

    def test_line_items_carry_no_tax_lines(self):
        payload, _options, _warnings = transform.order_to_graphql(self._order(), MigrationOptions(), None)
        for item in payload["lineItems"]:
            self.assertNotIn("taxLines", item, "a line item must not carry tax when the order already does")

    def test_order_level_tax_is_still_present(self):
        payload, _options, _warnings = transform.order_to_graphql(self._order(), MigrationOptions(), None)
        self.assertEqual(payload["taxLines"][0]["title"], "VAT")
        self.assertEqual(payload["taxLines"][0]["priceSet"]["shopMoney"]["amount"], "17.00")

    def test_rest_payload_is_consistent_too(self):
        payload, _warnings = transform.order_to_rest(self._order(), MigrationOptions(), None)
        for item in payload["line_items"]:
            self.assertNotIn("tax_lines", item)
        self.assertEqual(payload["tax_lines"][0]["title"], "VAT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
