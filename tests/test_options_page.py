"""Options page: the dynamic order-status list and its round trip through apply()."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from woo2shopify.config import AppConfig  # noqa: E402
from woo2shopify.ui.pages import OptionsPage  # noqa: E402

_app = QApplication.instance() or QApplication([])


class OptionsPageStatusTest(unittest.TestCase):
    def test_default_selection_excludes_failed_and_cancelled(self):
        config = AppConfig()
        page = OptionsPage(config)
        checked = {slug for slug, box in page.statusBoxes.items() if box.isChecked()}
        self.assertIn("completed", checked)
        self.assertNotIn("failed", checked)
        self.assertNotIn("cancelled", checked)

    def test_apply_reads_exactly_the_ticked_boxes(self):
        config = AppConfig()
        page = OptionsPage(config)
        page.statusBoxes["completed"].setChecked(False)
        page.statusBoxes["failed"].setChecked(True)
        page.apply(config)
        self.assertNotIn("completed", config.options.order_statuses)
        self.assertIn("failed", config.options.order_statuses)

    def test_refresh_preserves_the_current_ticks_by_slug(self):
        config = AppConfig()
        page = OptionsPage(config)
        page.statusBoxes["pending"].setChecked(True)   # user opts a non-default one in
        page.populate_statuses([
            {"slug": "completed", "name": "Completed", "total": 40},
            {"slug": "pending", "name": "Pending payment", "total": 3},
            {"slug": "custom-status", "name": "Awaiting supplier", "total": 5},
        ])
        self.assertTrue(page.statusBoxes["completed"].isChecked())
        self.assertTrue(page.statusBoxes["pending"].isChecked(), "a tick made before refresh must survive it")
        self.assertFalse(page.statusBoxes["custom-status"].isChecked(), "an unseen status must not default to checked")

    def test_select_all_and_none(self):
        config = AppConfig()
        page = OptionsPage(config)
        page._set_all_statuses(True)
        self.assertTrue(all(box.isChecked() for box in page.statusBoxes.values()))
        page._set_all_statuses(False)
        self.assertTrue(all(not box.isChecked() for box in page.statusBoxes.values()))

    def test_custom_status_label_includes_the_count(self):
        config = AppConfig()
        page = OptionsPage(config)
        page.populate_statuses([{"slug": "hazmana-supplier", "name": "Waiting on supplier", "total": 3}])
        self.assertIn("3", page.statusBoxes["hazmana-supplier"].text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
