"""SKU-matching fallback and fulfilment-location resolution.

Both were silent-failure modes in practice: a dirty SKU meant a perfectly
importable line item got downgraded to a custom line with no path to fix it,
and a store with no usable location meant every order came back unfulfilled
with nothing in the log explaining why.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from woo2shopify import transform  # noqa: E402
from woo2shopify.config import ShopifyConfig  # noqa: E402
from woo2shopify.shopify_api import ShopifyClient  # noqa: E402
from woo2shopify.state import StateStore  # noqa: E402


class SkuCandidatesTest(unittest.TestCase):
    def test_trailing_parenthetical_is_stripped_as_a_fallback(self):
        self.assertEqual(
            transform.sku_candidates("MISHEE-1 (מוצר 1)"),
            ["MISHEE-1 (מוצר 1)", "MISHEE-1"],
        )

    def test_exact_sku_always_comes_first(self):
        self.assertEqual(transform.sku_candidates("PLAIN-SKU")[0], "PLAIN-SKU")

    def test_blank_sku_yields_no_candidates(self):
        self.assertEqual(transform.sku_candidates(""), [])
        self.assertEqual(transform.sku_candidates("   "), [])

    def test_multiple_parentheticals_reduce_one_at_a_time(self):
        self.assertEqual(transform.sku_candidates("A (b) (c)"), ["A (b) (c)", "A (b)", "A"])


class VariantLookupFallbackTest(unittest.TestCase):
    """Exercised through StateStore directly — no network needed."""

    def setUp(self):
        self.state = StateStore(":memory:") if False else StateStore(Path("/tmp") / "w2s_test_lookup.sqlite3")
        self.state.reset(["variants", "variant_titles"])
        self.state.save_variants([{
            "sku": "MISHEE-1", "variant_gid": "gid://shopify/ProductVariant/1",
            "variant_id": "1", "product_gid": "gid://shopify/Product/1", "title": "Lip Glitter",
        }])

    def tearDown(self):
        self.state.close()

    def test_exact_sku_hits_directly(self):
        row = self.state.variant_by_sku("MISHEE-1")
        self.assertIsNotNone(row)

    def test_dirty_sku_matches_only_after_normalisation(self):
        dirty = "MISHEE-1 (מוצר 1)"
        self.assertIsNone(self.state.variant_by_sku(dirty), "the exact dirty SKU should not itself be in Shopify")
        candidates = transform.sku_candidates(dirty)
        matched = next((c for c in candidates if self.state.variant_by_sku(c)), None)
        self.assertEqual(matched, "MISHEE-1")


class PrimaryLocationTest(unittest.TestCase):
    def _client(self, log_sink=None):
        log = (lambda message, level="info": log_sink.append((level, message))) if log_sink is not None else None
        return ShopifyClient(ShopifyConfig(shop_domain="t.myshopify.com", access_token="shpat_x"), log=log)

    def _locations(self, *nodes):
        return {"locations": {"edges": [{"node": n} for n in nodes]}}

    def test_prefers_active_and_fulfilling(self):
        c = self._client()
        data = self._locations(
            {"id": "gid://shopify/Location/1", "name": "Warehouse", "isActive": True, "fulfillsOnlineOrders": False},
            {"id": "gid://shopify/Location/2", "name": "Shop", "isActive": True, "fulfillsOnlineOrders": True},
        )
        with mock.patch.object(c, "graphql", return_value=data):
            self.assertEqual(c.primary_location(), "gid://shopify/Location/2")

    def test_falls_back_to_any_active_location_with_a_warning(self):
        logs = []
        c = self._client(logs)
        data = self._locations(
            {"id": "gid://shopify/Location/1", "name": "Warehouse", "isActive": True, "fulfillsOnlineOrders": False},
        )
        with mock.patch.object(c, "graphql", return_value=data):
            self.assertEqual(c.primary_location(), "gid://shopify/Location/1")
        self.assertTrue(any(level == "warn" for level, _m in logs))

    def test_falls_back_to_inactive_location_with_a_warning(self):
        logs = []
        c = self._client(logs)
        data = self._locations(
            {"id": "gid://shopify/Location/9", "name": "Old Store", "isActive": False, "fulfillsOnlineOrders": False},
        )
        with mock.patch.object(c, "graphql", return_value=data):
            self.assertEqual(c.primary_location(), "gid://shopify/Location/9")
        self.assertTrue(any("inactive" in m for _l, m in logs))

    def test_no_locations_returns_blank_and_warns(self):
        logs = []
        c = self._client(logs)
        with mock.patch.object(c, "graphql", return_value=self._locations()):
            self.assertEqual(c.primary_location(), "")
        self.assertTrue(any("no locations" in m.lower() for _l, m in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
