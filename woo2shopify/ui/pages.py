"""The five pages of the migration app."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QHeaderView, QTableWidgetItem, QWidget
from qfluentwidgets import (
    isDarkTheme,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    TableWidget,
    TextEdit,
)

from ..config import APP_DIR, DEFAULT_ORDER_STATUSES, AppConfig
from ..oauth import DEFAULT_SCOPES, redirect_uri as oauth_redirect_uri
from .common import FormBlock, FormCard, PageBase, VCard, checkbox, combo, line_edit, row, spin

API_VERSIONS = ["2026-01", "2025-10", "2025-07", "2025-04", "2025-01", "2024-10", "2024-07"]
LEVEL_COLOURS = {"success": "#2e9e57", "warn": "#c8860d", "error": "#d64545"}
DARK_LEVEL_COLOURS = {"success": "#5fd08a", "warn": "#e0a83c", "error": "#ef6f6f"}


# --------------------------------------------------------------- connections
class ConnectionPage(PageBase):
    testRequested = pyqtSignal(str)
    saveRequested = pyqtSignal()
    oauthRequested = pyqtSignal()
    mintRequested = pyqtSignal()

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(
            "connectionPage",
            "Connections",
            "Credentials are stored locally in "
            f"{APP_DIR/'config.json'} with owner-only permissions. Nothing is sent anywhere else.",
            parent,
        )
        woo = config.woo
        shop = config.shopify

        card = FormCard("WooCommerce (source)", self)
        self.wooUrl = card.add("Store URL", line_edit("https://mystore.com", woo.base_url),
                               "The WordPress site root — the tool appends /wp-json/wc/v3 itself.")
        self.wooKey = card.add("Consumer key", line_edit("ck_...", woo.consumer_key),
                               "WooCommerce → Settings → Advanced → REST API → Add key (Read access is enough).")
        self.wooSecret = card.add("Consumer secret", line_edit("cs_...", woo.consumer_secret, password=True))
        self.wooVerify = checkbox("Verify the TLS certificate", woo.verify_ssl)
        self.wooQueryAuth = checkbox("Send credentials in the query string (for hosts that strip the auth header)",
                                     woo.query_string_auth)
        card.add_row(self.wooVerify)
        card.add_row(self.wooQueryAuth)
        self.wooTimeout = card.add("Request timeout (s)", spin(10, 600, woo.timeout))
        self.add_card(card)

        wp = FormCard("WordPress users (optional)", self)
        self.wpUser = wp.add("WordPress username", line_edit("admin", woo.wp_username),
                             "Only needed to import users who never placed an order (subscribers, staff…).")
        self.wpPass = wp.add("Application password", line_edit("xxxx xxxx xxxx xxxx", woo.wp_app_password, password=True),
                             "Users → Profile → Application Passwords.")
        self.add_card(wp)

        card = FormCard("Shopify (destination)", self)
        self.shopDomain = card.add("Shop domain", line_edit("my-store.myshopify.com", shop.shop_domain))
        self.shopToken = card.add("Admin API access token", line_edit("filled in by Mint token", shop.access_token, password=True),
                                  "Leave blank and use the card below if you only have a Client ID and secret. "
                                  "Scopes needed: write_customers, write_orders, read_products, read_locations.")
        self.apiVersion = card.add("API version", combo(API_VERSIONS, shop.api_version))
        self.authMode = card.add("Auth mode", combo(["client_credentials", "token"], shop.auth_mode),
                                 "client_credentials: the tool mints its own tokens from the app's Client ID "
                                 "and secret, and renews them every 24h. token: use the token above as-is.")
        self.orderApi = card.add("Order API", combo(["graphql", "rest"], shop.order_api),
                                 "GraphQL is the default. Switch to REST if your store rejects orderCreate.")
        self.locationId = card.add("Location ID (optional)", line_edit("gid://shopify/Location/123", shop.location_id),
                                   "Used when marking imported orders fulfilled. Detected automatically if blank.")
        self.add_card(card)

        card = VCard("App credentials (Dev Dashboard apps)", self)
        blurb = BodyLabel(
            "A Dev Dashboard app never shows you an Admin API token — it gives you a Client ID "
            "and Client secret, which have to be exchanged for one. Enter them here.\n\n"
            "Mint token: one request, no browser. Works when the app and the store are in the "
            "same Shopify organization — an app you built and installed on your own store. "
            "These tokens last 24 hours, so leave Auth mode on client_credentials and the tool "
            "renews them mid-run by itself.\n\n"
            "Browser OAuth: for a store outside the app's organization, such as a client's shop. "
            f"Register {oauth_redirect_uri()} as an allowed redirect URL on the app first. "
            "It returns a token that does not expire.", card)
        blurb.setWordWrap(True)
        card.add_widget(blurb)
        form = FormBlock(card)
        self.clientId = form.add("Client ID", line_edit("eb43e61e71bf...", shop.client_id))
        self.clientSecret = form.add("Client secret", line_edit("", shop.client_secret, password=True))
        self.scopes = form.add("Scopes (browser OAuth only)", line_edit(DEFAULT_SCOPES, DEFAULT_SCOPES),
                               "With client credentials the scopes come from the app version instead.")
        self.oauthPort = form.add("Callback port (browser OAuth only)", spin(1024, 65535, shop.oauth_port),
                                  "Must match the port in the redirect URL registered on the app.")
        card.add_widget(form)
        self.mintBtn = PrimaryPushButton(FluentIcon.CERTIFICATE, "Mint token")
        self.oauthBtn = PushButton(FluentIcon.GLOBE, "Browser OAuth instead")
        self.mintBtn.clicked.connect(self.mintRequested.emit)
        self.oauthBtn.clicked.connect(self.oauthRequested.emit)
        card.add_widget(row(self.mintBtn, self.oauthBtn))
        self.add_card(card)

        self.wooTestBtn = PushButton(FluentIcon.SYNC, "Test WooCommerce")
        self.shopTestBtn = PushButton(FluentIcon.SYNC, "Test Shopify")
        self.saveBtn = PrimaryPushButton(FluentIcon.SAVE, "Save settings")
        self.wooTestBtn.clicked.connect(lambda: self.testRequested.emit("woo"))
        self.shopTestBtn.clicked.connect(lambda: self.testRequested.emit("shopify"))
        self.saveBtn.clicked.connect(self.saveRequested.emit)
        self.add_card(row(self.wooTestBtn, self.shopTestBtn, self.saveBtn))

    def apply(self, config: AppConfig) -> None:
        woo = config.woo
        woo.base_url = self.wooUrl.text().strip()
        woo.consumer_key = self.wooKey.text().strip()
        woo.consumer_secret = self.wooSecret.text().strip()
        woo.verify_ssl = self.wooVerify.isChecked()
        woo.query_string_auth = self.wooQueryAuth.isChecked()
        woo.timeout = self.wooTimeout.value()
        woo.wp_username = self.wpUser.text().strip()
        woo.wp_app_password = self.wpPass.text().strip()

        shop = config.shopify
        shop.shop_domain = self.shopDomain.text().strip()
        shop.access_token = self.shopToken.text().strip()
        shop.api_version = self.apiVersion.currentText()
        shop.order_api = self.orderApi.currentText()
        shop.location_id = self.locationId.text().strip()
        shop.client_id = self.clientId.text().strip()
        shop.client_secret = self.clientSecret.text().strip()
        shop.oauth_port = self.oauthPort.value()
        shop.auth_mode = self.authMode.currentText()

    def set_token(self, token: str, auth_mode: str = "") -> None:
        self.shopToken.setText(token)
        if auth_mode:
            index = self.authMode.findText(auth_mode)
            if index >= 0:
                self.authMode.setCurrentIndex(index)

    def set_location(self, gid: str) -> None:
        if gid and not self.locationId.text().strip():
            self.locationId.setText(gid)


# -------------------------------------------------------------------- options
class OptionsPage(PageBase):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(
            "optionsPage",
            "What to migrate",
            "Products are assumed to be in Shopify already — line items are matched back to them by SKU.",
            parent,
        )
        opts = config.options

        card = FormCard("Records", self)
        self.migCustomers = checkbox("Customers (registered WooCommerce customers)", opts.migrate_customers)
        self.migOrders = checkbox("Orders", opts.migrate_orders)
        self.guestCustomers = checkbox("Create customers from guest orders too", opts.include_guest_customers)
        self.wpUsers = checkbox("Also import WordPress users without orders (needs an app password)", opts.include_wp_users)
        self.orderNotes = checkbox("Copy order notes into the Shopify order note", opts.import_order_notes)
        self.fulfillments = checkbox("Mark completed orders as fulfilled", opts.import_fulfillments)
        self.transactions = checkbox("Create a matching sale transaction for paid orders", opts.import_transactions)
        for widget in (self.migCustomers, self.migOrders, self.guestCustomers, self.wpUsers,
                       self.orderNotes, self.fulfillments, self.transactions):
            card.add_row(widget)
        self.add_card(card)

        card = FormCard("Date range", self)
        self.years = card.add("Years of history", spin(1, 30, opts.years_back),
                              "Ignored when an explicit start date is set below.")
        self.dateFrom = card.add("From (YYYY-MM-DD)", line_edit("2021-01-01", opts.date_from))
        self.dateTo = card.add("To (YYYY-MM-DD)", line_edit("leave blank for today", opts.date_to))
        self.add_card(card)

        card = VCard("Order statuses", self)
        holder = QWidget(card)
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        self.statusBoxes: Dict[str, CheckBox] = {}
        for index, status in enumerate(DEFAULT_ORDER_STATUSES):
            box = CheckBox(status, holder)
            box.setChecked(status in opts.order_statuses)
            self.statusBoxes[status] = box
            grid.addWidget(box, index // 4, index % 4)
        card.add_widget(holder)
        self.add_card(card)

        card = FormCard("Product matching", self)
        self.matchBy = card.add("Match line items by", combo(["sku", "sku_then_title", "none"], opts.match_by),
                                "SKU is the reliable one. 'none' imports every line as a custom line item.")
        self.fallbackLines = checkbox("Keep unmatched lines as custom line items (recommended — keeps order totals right)",
                                      opts.fallback_custom_line_items)
        card.add_row(self.fallbackLines)
        self.add_card(card)

        card = FormCard("Behaviour", self)
        self.dryRun = checkbox("Dry run — read everything, write nothing", opts.dry_run)
        self.resume = checkbox("Resume: skip records already imported in an earlier run", opts.resume)
        self.preserveNumbers = checkbox("Keep the WooCommerce order number as the Shopify order name", opts.preserve_order_numbers)
        self.sendReceipts = checkbox("Let Shopify email receipts (leave off for historical imports)", opts.send_receipts)
        self.skipZero = checkbox("Skip zero-total orders", opts.skip_zero_total)
        self.marketing = checkbox("Honour the WooCommerce marketing opt-in flag", opts.marketing_consent)
        self.metafields = checkbox("Store WooCommerce ids in metafields", opts.store_woo_metafields)
        for widget in (self.dryRun, self.resume, self.preserveNumbers, self.sendReceipts,
                       self.skipZero, self.marketing, self.metafields):
            card.add_row(widget)
        self.inventory = card.add("Inventory behaviour",
                                  combo(["BYPASS", "DECREMENT_IGNORING_POLICY", "DECREMENT_OBEYING_POLICY"],
                                        opts.inventory_behaviour),
                                  "BYPASS leaves today's stock levels alone — what you want for history.")
        self.customerTag = card.add("Customer tag", line_edit("woo-import", opts.customer_tag))
        self.orderTag = card.add("Order tag", line_edit("woo-import", opts.order_tag))
        self.gateway = card.add("Fallback payment gateway", line_edit("manual", opts.default_gateway))
        self.pageSize = card.add("WooCommerce page size", spin(10, 100, opts.page_size))
        self.windowDays = card.add("Date window (days per query)", spin(1, 365, opts.window_days),
                                   "Smaller windows page more reliably on big stores.")
        self.retries = card.add("Max retries", spin(1, 20, opts.max_retries))
        self.delay = card.add("Extra delay between writes (ms)", spin(0, 5000, int(opts.request_delay * 1000)))
        self.add_card(card)

    def apply(self, config: AppConfig) -> None:
        opts = config.options
        opts.migrate_customers = self.migCustomers.isChecked()
        opts.migrate_orders = self.migOrders.isChecked()
        opts.include_guest_customers = self.guestCustomers.isChecked()
        opts.include_wp_users = self.wpUsers.isChecked()
        opts.import_order_notes = self.orderNotes.isChecked()
        opts.import_fulfillments = self.fulfillments.isChecked()
        opts.import_transactions = self.transactions.isChecked()

        opts.years_back = self.years.value()
        opts.date_from = self.dateFrom.text().strip()
        opts.date_to = self.dateTo.text().strip()
        opts.order_statuses = [s for s, box in self.statusBoxes.items() if box.isChecked()]

        opts.match_by = self.matchBy.currentText()
        opts.match_variants = opts.match_by != "none"
        opts.fallback_custom_line_items = self.fallbackLines.isChecked()

        opts.dry_run = self.dryRun.isChecked()
        opts.resume = self.resume.isChecked()
        opts.preserve_order_numbers = self.preserveNumbers.isChecked()
        opts.send_receipts = self.sendReceipts.isChecked()
        opts.skip_zero_total = self.skipZero.isChecked()
        opts.marketing_consent = self.marketing.isChecked()
        opts.store_woo_metafields = self.metafields.isChecked()
        opts.inventory_behaviour = self.inventory.currentText()
        opts.customer_tag = self.customerTag.text().strip() or "woo-import"
        opts.order_tag = self.orderTag.text().strip() or "woo-import"
        opts.default_gateway = self.gateway.text().strip() or "manual"
        opts.page_size = self.pageSize.value()
        opts.window_days = self.windowDays.value()
        opts.max_retries = self.retries.value()
        opts.request_delay = self.delay.value() / 1000.0


# ------------------------------------------------------------------- products
class ProductsPage(PageBase):
    buildRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(
            "productsPage",
            "Product map",
            "Orders reference products by SKU. Build this index once (and again whenever the catalogue changes) "
            "so imported line items link to the real Shopify variants.",
            parent,
        )
        card = VCard("SKU → variant index", self)
        self.countLabel = StrongBodyLabel("No variants indexed yet.", card)
        self.buildBtn = PrimaryPushButton(FluentIcon.SYNC, "Build / refresh index")
        self.buildBtn.clicked.connect(self.buildRequested.emit)
        card.add_widget(self.countLabel)
        card.add_widget(row(self.buildBtn))
        self.add_card(card)

        self.table = TableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["SKU", "Variant", "Variant ID"])
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(360)
        self.add_card(self.table)

    def set_count(self, count: int) -> None:
        self.countLabel.setText(
            f"{count} SKUs indexed." if count else "No variants indexed yet — orders would import as custom line items."
        )

    def show_rows(self, rows: List[Any]) -> None:
        self.table.setRowCount(len(rows))
        for r, data in enumerate(rows):
            for c, value in enumerate((data["sku"], data["title"], data["variant_id"])):
                self.table.setItem(r, c, QTableWidgetItem(str(value or "")))


# ------------------------------------------------------------------------ run
class RunPage(PageBase):
    startRequested = pyqtSignal(str)
    pauseRequested = pyqtSignal()
    stopRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("runPage", "Run migration",
                         "Safe to stop and restart — every imported record is checkpointed locally.", parent)

        card = VCard("Controls", self)
        self.startBtn = PrimaryPushButton(FluentIcon.PLAY, "Start full migration")
        self.customersBtn = PushButton(FluentIcon.PEOPLE, "Customers only")
        self.ordersBtn = PushButton(FluentIcon.SHOPPING_CART, "Orders only")
        self.pauseBtn = PushButton(FluentIcon.PAUSE, "Pause")
        self.stopBtn = PushButton(FluentIcon.CANCEL, "Stop")
        self.pauseBtn.setEnabled(False)
        self.stopBtn.setEnabled(False)
        self.startBtn.clicked.connect(lambda: self.startRequested.emit("run"))
        self.customersBtn.clicked.connect(lambda: self.startRequested.emit("customers"))
        self.ordersBtn.clicked.connect(lambda: self.startRequested.emit("orders"))
        self.pauseBtn.clicked.connect(self.pauseRequested.emit)
        self.stopBtn.clicked.connect(self.stopRequested.emit)
        card.add_widget(
            row(self.startBtn, self.customersBtn, self.ordersBtn, self.pauseBtn, self.stopBtn)
        )
        self.add_card(card)

        card = VCard("Progress", self)
        self.bars: Dict[str, ProgressBar] = {}
        self.barLabels: Dict[str, BodyLabel] = {}
        for phase, label in (("variants", "Product index"), ("customers", "Customers"), ("orders", "Orders")):
            holder = QWidget(card)
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            name = BodyLabel(label, holder)
            name.setMinimumWidth(110)
            bar = ProgressBar(holder)
            bar.setValue(0)
            value = BodyLabel("0", holder)
            value.setMinimumWidth(120)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            layout.addWidget(name)
            layout.addWidget(bar, 1)
            layout.addWidget(value)
            card.add_widget(holder)
            self.bars[phase] = bar
            self.barLabels[phase] = value
        self.statsLabel = CaptionLabel("Idle.", card)
        self.statsLabel.setWordWrap(True)
        card.add_widget(self.statsLabel)
        self.add_card(card)

        card = VCard("Log", self)
        self.log = TextEdit(card)
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(260)
        card.add_widget(self.log)
        self.add_card(card)

        card = VCard("Failures", self)
        self.errorTable = TableWidget(card)
        self.errorTable.setColumnCount(4)
        self.errorTable.setHorizontalHeaderLabels(["Type", "Woo ID", "Reference", "Error"])
        self.errorTable.verticalHeader().hide()
        self.errorTable.setBorderVisible(True)
        self.errorTable.setBorderRadius(8)
        self.errorTable.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.errorTable.setMinimumHeight(200)
        card.add_widget(self.errorTable)
        self.add_card(card)

    def append_log(self, message: str, level: str = "info") -> None:
        palette = DARK_LEVEL_COLOURS if isDarkTheme() else LEVEL_COLOURS
        safe = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        colour = palette.get(level)
        self.log.append(f'<span style="color:{colour}">{safe}</span>' if colour else safe)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def set_progress(self, phase: str, done: int, total: int) -> None:
        bar = self.bars.get(phase)
        if bar is None:
            return
        if total:
            bar.setRange(0, total)
            bar.setValue(min(done, total))
            self.barLabels[phase].setText(f"{done} / {total}")
        else:
            bar.setRange(0, 0)
            self.barLabels[phase].setText(str(done))

    def set_stats(self, stats: Dict[str, int]) -> None:
        self.statsLabel.setText(
            "Customers: {customers_created} created, {customers_existing} already there, "
            "{customers_failed} failed, {customers_skipped} skipped   |   "
            "Orders: {orders_created} created, {orders_existing} already there, "
            "{orders_failed} failed, {orders_skipped} skipped   |   {warnings} warnings".format(
                **{**{k: 0 for k in (
                    "customers_created", "customers_existing", "customers_failed", "customers_skipped",
                    "orders_created", "orders_existing", "orders_failed", "orders_skipped", "warnings")},
                   **stats}
            )
        )

    def add_failure(self, record: Dict[str, Any]) -> None:
        r = self.errorTable.rowCount()
        self.errorTable.insertRow(r)
        for c, key in enumerate(("type", "woo_id", "ref", "error")):
            item = QTableWidgetItem(str(record.get(key, "")))
            if key == "error":
                item.setForeground(QColor((DARK_LEVEL_COLOURS if isDarkTheme() else LEVEL_COLOURS)["error"]))
            self.errorTable.setItem(r, c, item)
        self.errorTable.scrollToBottom()

    def set_running(self, running: bool) -> None:
        for btn in (self.startBtn, self.customersBtn, self.ordersBtn):
            btn.setEnabled(not running)
        self.pauseBtn.setEnabled(running)
        self.stopBtn.setEnabled(running)
        if not running:
            self.pauseBtn.setText("Pause")


# -------------------------------------------------------------------- reports
class ReportsPage(PageBase):
    exportRequested = pyqtSignal()
    refreshRequested = pyqtSignal()
    resetRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("reportsPage", "Reports",
                         "Every record that was created, skipped or failed is kept in a local SQLite file.", parent)
        card = VCard("Summary", self)
        self.summary = BodyLabel("Nothing recorded yet.", card)
        self.summary.setWordWrap(True)
        card.add_widget(self.summary)
        self.refreshBtn = PushButton(FluentIcon.SYNC, "Refresh")
        self.exportBtn = PrimaryPushButton(FluentIcon.DOWNLOAD, "Export CSV reports")
        self.openBtn = PushButton(FluentIcon.FOLDER, "Open data folder")
        self.resetBtn = PushButton(FluentIcon.DELETE, "Reset migration state")
        self.refreshBtn.clicked.connect(self.refreshRequested.emit)
        self.exportBtn.clicked.connect(self.exportRequested.emit)
        self.resetBtn.clicked.connect(self.resetRequested.emit)
        self.openBtn.clicked.connect(lambda: open_folder(APP_DIR))
        card.add_widget(row(self.refreshBtn, self.exportBtn, self.openBtn, self.resetBtn))
        self.add_card(card)

        card = VCard("Failed records", self)
        self.table = TableWidget(card)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Table", "Woo ID", "Reference", "Error"])
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMinimumHeight(320)
        card.add_widget(self.table)
        self.add_card(card)

    def set_summary(self, counts: Dict[str, int]) -> None:
        self.summary.setText(
            f"Customers — done {counts.get('customers_done', 0)}, failed {counts.get('customers_failed', 0)}, "
            f"skipped {counts.get('customers_skipped', 0)}\n"
            f"Orders — done {counts.get('orders_done', 0)}, failed {counts.get('orders_failed', 0)}, "
            f"skipped {counts.get('orders_skipped', 0)}\n"
            f"Indexed variants — {counts.get('variants', 0)}\n"
            f"Data folder: {APP_DIR}"
        )

    def set_failures(self, rows: List[Any]) -> None:
        self.table.setRowCount(len(rows))
        for r, (table, woo_id, ref, error) in enumerate(rows):
            for c, value in enumerate((table, woo_id, ref, error)):
                self.table.setItem(r, c, QTableWidgetItem(str(value or "")))


def open_folder(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass
