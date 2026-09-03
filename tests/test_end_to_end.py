"""End-to-end run of the migrator against mock WooCommerce and Shopify APIs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_server import start_server  # noqa: E402
from woo2shopify import transform  # noqa: E402
from woo2shopify.config import AppConfig, ShopifyConfig  # noqa: E402
from woo2shopify.migrator import Migrator, Reporter  # noqa: E402


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.recorder = start_server()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

        # Point the Shopify client at the mock server instead of *.myshopify.com.
        ShopifyConfig.graphql_url = property(lambda self: f"{MigrationTest.base}/admin/api/x/graphql.json")
        ShopifyConfig.rest_url = lambda self, path: f"{MigrationTest.base}/admin/api/x/{path.lstrip('/')}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        # each test starts from an empty destination store
        self.recorder.customers.clear()
        self.recorder.orders.clear()
        self.recorder.notes.clear()
        self.recorder.existing_emails.clear()

    def build(self, **option_overrides):
        config = AppConfig()
        config.woo.base_url = self.base
        config.woo.consumer_key = "ck"
        config.woo.consumer_secret = "cs"
        config.shopify.shop_domain = "test.myshopify.com"
        config.shopify.access_token = "shpat_test"
        config.options.years_back = 10
        config.options.match_by = "sku"
        for key, value in option_overrides.items():
            setattr(config.options, key, value)
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        logs = []
        reporter = Reporter(on_log=lambda msg, level="info": logs.append((level, msg)))
        migrator = Migrator(config, reporter=reporter, state_path=Path(self.tmp.name))
        return migrator, logs

    def test_full_run(self):
        migrator, logs = self.build()
        stats = migrator.run()

        self.assertEqual(stats["customers_failed"], 0, [m for l, m in logs if l == "error"])
        self.assertEqual(stats["orders_failed"], 0, [m for l, m in logs if l == "error"])
        # 2 customers with an email, 1 skipped for having none, 1 guest created on the fly
        self.assertEqual(stats["customers_created"], 3)
        self.assertEqual(stats["customers_skipped"], 1)
        self.assertEqual(stats["orders_created"], 4)

        orders = {o["customAttributes"][0]["value"]: o for o in self.recorder.orders}
        self.assertEqual(set(orders), {"501", "502", "503", "504"})

        first = orders["501"]
        self.assertEqual(first["currency"], "ILS")
        self.assertEqual(first["financialStatus"], "PAID")
        self.assertTrue(first["taxesIncluded"])
        self.assertIn("woo-order-501", first["tags"])
        self.assertEqual(first["processedAt"], "2022-03-04T10:15:00Z")
        self.assertEqual(first["customer"], {"toAssociate": {"id": "gid://shopify/Customer/7001"}})

        # matched by SKU, unmatched line kept, fee turned into its own line
        titles = [li["title"] for li in first["lineItems"]]
        self.assertEqual(titles, ["Blue Mug", "Vanished Product", "Gift wrap"])
        self.assertEqual(first["lineItems"][0]["variantId"], "gid://shopify/ProductVariant/1001")
        self.assertNotIn("variantId", first["lineItems"][1])
        self.assertEqual(first["lineItems"][0]["priceSet"]["shopMoney"]["amount"], "100.00")
        self.assertEqual(first["lineItems"][0]["properties"], [{"name": "Engraving", "value": "Yes"}])

        self.assertEqual(first["shippingLines"][0]["priceSet"]["shopMoney"]["amount"], "20.00")
        self.assertEqual(first["taxLines"][0]["title"], "VAT")
        self.assertAlmostEqual(first["taxLines"][0]["rate"], 0.17)
        self.assertEqual(first["discountCode"]["itemFixedDiscountCode"]["code"], "SPRING10")
        self.assertEqual(first["fulfillment"], {"locationId": "gid://shopify/Location/55", "notifyCustomer": False})
        self.assertEqual(first["transactions"][0]["amountSet"]["shopMoney"]["amount"], "234.00")

        self.assertEqual(orders["502"]["financialStatus"], "REFUNDED")
        self.assertNotIn("transactions", orders["502"])
        self.assertEqual(orders["504"]["lineItems"][0]["title"], "WooCommerce order #504")
        self.assertIn("closedAt", orders["504"])

        # guest order got its own customer
        guest = [c for c in self.recorder.customers if c.get("email") == "guest@example.com"]
        self.assertEqual(len(guest), 1)
        self.assertIn("woo-guest", guest[0]["tags"])

        # order notes copied over
        self.assertTrue(any("Imported note for order 501" in n["note"] for n in self.recorder.notes))
        migrator.close()

    def test_resume_is_idempotent(self):
        migrator, _ = self.build()
        first = dict(migrator.run())
        before = len(self.recorder.orders)
        second = migrator.run()
        # the counters accumulate on the instance, so "no growth" means nothing new was written
        self.assertEqual(second["orders_created"], first["orders_created"])
        self.assertEqual(second["customers_created"], first["customers_created"])
        self.assertEqual(len(self.recorder.orders), before)
        migrator.close()

    def test_dry_run_writes_nothing(self):
        migrator, _ = self.build(dry_run=True)
        created_before = len(self.recorder.orders)
        stats = migrator.run()
        self.assertEqual(len(self.recorder.orders), created_before)
        self.assertEqual(stats["orders_created"], 0)
        self.assertGreater(stats["orders_skipped"], 0)
        migrator.close()

    def test_rest_order_payload(self):
        migrator, logs = self.build()
        migrator.cfg.shopify.order_api = "rest"
        migrator.opts.migrate_customers = False
        migrator.build_variant_map()
        migrator.migrate_orders()
        rest_orders = [o for o in self.recorder.orders if "line_items" in o]
        self.assertEqual(len(rest_orders), 4, [m for l, m in logs if l == "error"])
        sample = next(o for o in rest_orders if "woo-order-501" in o["tags"])
        self.assertEqual(sample["financial_status"], "paid")
        self.assertEqual(sample["line_items"][0]["variant_id"], 1001)
        self.assertEqual(sample["fulfillment_status"], "fulfilled")
        self.assertEqual(sample["inventory_behaviour"], "bypass")
        migrator.close()

    def test_phone_retry_drops_bad_phone(self):
        """Shopify rejects a non-E.164 phone; the retry must strip it, not fail."""
        migrator, logs = self.build(migrate_orders=False)
        migrator.run()
        sam = next(c for c in self.recorder.customers if c.get("email") == "sam@example.com")
        self.assertNotIn("phone", sam)
        self.assertEqual([m for l, m in logs if l == "error"], [])
        migrator.close()


class TransformTest(unittest.TestCase):
    def test_iso_and_money(self):
        self.assertEqual(transform.iso_z("2021-04-02T10:11:12"), "2021-04-02T10:11:12Z")
        self.assertEqual(transform.iso_z("2021-04-02T10:11:12+03:00"), "2021-04-02T07:11:12Z")
        self.assertIsNone(transform.iso_z(""))
        self.assertEqual(transform.money_str("3.5"), "3.50")
        self.assertEqual(transform.money_str(None), "0.00")

    def test_phone_normalisation(self):
        self.assertEqual(transform.normalize_phone("00972 54 123 4567"), "+972541234567")
        self.assertEqual(transform.normalize_phone("054-1234567"), "")

    def test_address_province_handling(self):
        us = transform.address_input({"address_1": "1 A", "city": "Austin", "state": "TX", "country": "US"})
        self.assertEqual(us["provinceCode"], "TX")
        named = transform.address_input({"address_1": "1 A", "city": "Lyon", "state": "Rhone", "country": "FR"})
        self.assertNotIn("provinceCode", named)
        self.assertEqual(named["countryCode"], "FR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
