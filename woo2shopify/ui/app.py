"""Main window: wires the pages to the migrator worker."""

from __future__ import annotations

import sys
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import (
    FluentIcon,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    setTheme,
    setThemeColor,
    Theme,
)

from ..config import AppConfig, CONFIG_PATH, ensure_dirs
from ..state import StateStore
from .pages import ConnectionPage, OptionsPage, ProductsPage, ReportsPage, RunPage
from .worker import ConnectionTestWorker, MigrationWorker, TokenWorker


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        ensure_dirs()
        self.config = AppConfig.load()
        self.worker: Optional[MigrationWorker] = None
        self.testWorker: Optional[ConnectionTestWorker] = None
        self.tokenWorker: Optional[TokenWorker] = None
        self._tokenMode = ""

        self.connectionPage = ConnectionPage(self.config, self)
        self.optionsPage = OptionsPage(self.config, self)
        self.productsPage = ProductsPage(self)
        self.runPage = RunPage(self)
        self.reportsPage = ReportsPage(self)

        self._init_navigation()
        self._init_window()
        self._connect_signals()
        self.refresh_reports()

    # ----------------------------------------------------------------- setup
    def _init_navigation(self) -> None:
        self.addSubInterface(self.connectionPage, FluentIcon.LINK, "Connections")
        self.addSubInterface(self.optionsPage, FluentIcon.SETTING, "Options")
        self.addSubInterface(self.productsPage, FluentIcon.TILES, "Product map")
        self.addSubInterface(self.runPage, FluentIcon.PLAY, "Run")
        self.addSubInterface(
            self.reportsPage, FluentIcon.DOCUMENT, "Reports", position=NavigationItemPosition.BOTTOM
        )

    def _init_window(self) -> None:
        self.resize(1180, 840)
        self.setMinimumSize(980, 700)
        self.setWindowTitle("WooCommerce → Shopify migration")
        self.setWindowIcon(QIcon())
        setTheme(Theme.AUTO)
        setThemeColor("#5b8def")
        desktop = QApplication.desktop().availableGeometry()
        self.move((desktop.width() - self.width()) // 2, max(0, (desktop.height() - self.height()) // 2))

    def _connect_signals(self) -> None:
        self.connectionPage.testRequested.connect(self.test_connection)
        self.connectionPage.saveRequested.connect(self.save_config)
        self.connectionPage.oauthRequested.connect(lambda: self.mint_token("browser"))
        self.connectionPage.mintRequested.connect(lambda: self.mint_token("client_credentials"))
        self.productsPage.buildRequested.connect(lambda: self.start_task("variants"))
        self.runPage.startRequested.connect(self.start_task)
        self.runPage.pauseRequested.connect(self.toggle_pause)
        self.runPage.stopRequested.connect(self.stop_task)
        self.reportsPage.refreshRequested.connect(self.refresh_reports)
        self.reportsPage.exportRequested.connect(self.export_reports)
        self.reportsPage.resetRequested.connect(self.reset_state)

    # ---------------------------------------------------------------- config
    def collect_config(self) -> AppConfig:
        self.connectionPage.apply(self.config)
        self.optionsPage.apply(self.config)
        return self.config

    def save_config(self) -> None:
        self.collect_config().save()
        self.toast("Saved", f"Settings written to {CONFIG_PATH}", "success")

    def validate(self, need_woo: bool = True, need_shopify: bool = True) -> bool:
        config = self.collect_config()
        if need_woo and not (config.woo.base_url and config.woo.consumer_key and config.woo.consumer_secret):
            self.toast("Missing WooCommerce credentials", "Fill in the store URL, consumer key and secret.", "error")
            return False
        if need_shopify and not (config.shopify.shop_domain and config.shopify.access_token):
            self.toast("Missing Shopify credentials", "Fill in the shop domain and Admin API token.", "error")
            return False
        return True

    # ------------------------------------------------------------ connection
    def test_connection(self, target: str) -> None:
        if not self.validate(need_woo=target == "woo", need_shopify=target != "woo"):
            return
        if self.testWorker and self.testWorker.isRunning():
            return
        self.config.save()
        self.testWorker = ConnectionTestWorker(self.config, target, self)
        self.testWorker.sig_result.connect(self.on_test_result)
        self.testWorker.start()
        self.toast("Testing…", f"Contacting {'WooCommerce' if target == 'woo' else 'Shopify'}.", "info")

    def on_test_result(self, target: str, ok: bool, message: str, payload: dict) -> None:
        self.toast("Connected" if ok else "Connection failed", message, "success" if ok else "error")
        self.runPage.append_log(message, "success" if ok else "error")
        if ok and target == "shopify" and payload.get("location"):
            self.connectionPage.set_location(payload["location"])

    def mint_token(self, mode: str) -> None:
        config = self.collect_config()
        if not config.shopify.shop_domain:
            self.toast("Missing shop domain", "Enter my-store.myshopify.com first.", "error")
            return
        if not (config.shopify.client_id and config.shopify.client_secret):
            self.toast("Missing app credentials", "Enter the app's Client ID and Client secret.", "error")
            return
        if self.tokenWorker and self.tokenWorker.isRunning():
            return
        config.save()
        self._set_token_buttons(False)
        self.switchTo(self.runPage)
        self.runPage.append_log(
            "Approve the app in your browser to continue." if mode == "browser"
            else "Requesting an access token with the app's credentials…", "info")
        self._tokenMode = mode
        self.tokenWorker = TokenWorker(config, mode, self.connectionPage.scopes.text().strip(), self)
        self.tokenWorker.sig_log.connect(self.runPage.append_log)
        self.tokenWorker.sig_result.connect(self.on_token_result)
        self.tokenWorker.start()

    def _set_token_buttons(self, enabled: bool) -> None:
        self.connectionPage.mintBtn.setEnabled(enabled)
        self.connectionPage.oauthBtn.setEnabled(enabled)

    def on_token_result(self, ok: bool, token: str, message: str) -> None:
        self._set_token_buttons(True)
        self.runPage.append_log(message, "success" if ok else "error")
        if ok:
            # a client-credentials token dies in 24h, so keep the renewing mode on
            mode = "client_credentials" if getattr(self, "_tokenMode", "") == "client_credentials" else "token"
            self.connectionPage.set_token(token, mode)
            self.config.shopify.access_token = token
            self.config.shopify.auth_mode = mode
            self.config.save()
            self.switchTo(self.connectionPage)
            self.toast("Token ready", message + ". Try 'Test Shopify'.", "success")
        else:
            self.toast("Could not get a token", message, "error")

    # --------------------------------------------------------------- running
    def start_task(self, task: str) -> None:
        if self.worker and self.worker.isRunning():
            self.toast("Already running", "Stop the current run before starting another.", "warn")
            return
        need_woo = task != "variants"
        if not self.validate(need_woo=need_woo, need_shopify=True):
            return
        config = self.collect_config()
        config.save()

        if task in ("run", "orders") and config.options.dry_run:
            self.runPage.append_log("Dry run enabled — nothing will be written to Shopify.", "warn")

        self.switchTo(self.runPage)
        self.runPage.append_log(f"--- starting: {task} ---", "success")
        self.runPage.set_running(True)

        self.worker = MigrationWorker(config, task, self)
        self.worker.sig_log.connect(self.runPage.append_log)
        self.worker.sig_progress.connect(self.runPage.set_progress)
        self.worker.sig_stats.connect(self.runPage.set_stats)
        self.worker.sig_record.connect(self.runPage.add_failure)
        self.worker.sig_finished.connect(self.on_task_finished)
        self.worker.start()

    def toggle_pause(self) -> None:
        if not (self.worker and self.worker.isRunning()):
            return
        if self.worker.control.pause_event.is_set():
            self.worker.resume()
            self.runPage.pauseBtn.setText("Pause")
            self.runPage.append_log("Resumed.", "info")
        else:
            self.worker.pause()
            self.runPage.pauseBtn.setText("Resume")
            self.runPage.append_log("Paused — finishing the record in flight.", "warn")

    def stop_task(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.runPage.append_log("Stop requested…", "warn")

    def on_task_finished(self, task: str, ok: bool) -> None:
        self.runPage.set_running(False)
        self.runPage.append_log(f"--- finished: {task} ---", "success" if ok else "error")
        self.toast("Done" if ok else "Finished with errors",
                   f"Task '{task}' finished.", "success" if ok else "error")
        self.refresh_reports()

    # --------------------------------------------------------------- reports
    def _store(self) -> StateStore:
        return StateStore()

    def refresh_reports(self) -> None:
        store = self._store()
        try:
            counts = store.counts()
            self.reportsPage.set_summary(counts)
            self.productsPage.set_count(counts.get("variants", 0))
            rows = []
            for table in ("customers", "orders"):
                for record in store.failures(table)[:500]:
                    ref = record["email"] if table == "customers" else record["woo_number"]
                    rows.append((table, record["woo_id"], ref, record["error"]))
            self.reportsPage.set_failures(rows)
            preview = []
            with store._lock:
                for record in store._conn.execute(
                    "SELECT sku, title, variant_id FROM variants ORDER BY sku LIMIT 300"
                ).fetchall():
                    preview.append({"sku": record["sku"], "title": record["title"], "variant_id": record["variant_id"]})
            self.productsPage.show_rows(preview)
        finally:
            store.close()

    def export_reports(self) -> None:
        from ..config import EXPORT_DIR
        from datetime import datetime

        store = self._store()
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            paths = [str(store.dump_csv(table, EXPORT_DIR / f"{table}-{stamp}.csv"))
                     for table in ("customers", "orders", "variants")]
        finally:
            store.close()
        self.toast("Exported", "\n".join(paths), "success")

    def reset_state(self) -> None:
        box = MessageBox(
            "Reset migration state?",
            "This clears the local record of what was already imported. Nothing in Shopify is deleted, "
            "but a new run will try to re-import everything (already-imported orders are still detected "
            "by their woo-order-<id> tag).",
            self,
        )
        if not box.exec():
            return
        store = self._store()
        try:
            store.reset(["customers", "orders", "email_map", "variants", "variant_titles", "meta"])
        finally:
            store.close()
        self.refresh_reports()
        self.toast("Reset", "Local migration state cleared.", "success")

    # ----------------------------------------------------------------- misc
    def toast(self, title: str, content: str, level: str = "info") -> None:
        factory = {
            "success": InfoBar.success,
            "warn": InfoBar.warning,
            "error": InfoBar.error,
        }.get(level, InfoBar.info)
        factory(
            title=title,
            content=content,
            orient=Qt.Vertical if len(content) > 90 else Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=6000 if level in ("error", "warn") else 3500,
            parent=self,
        )

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            box = MessageBox("A migration is running", "Stop it and quit?", self)
            if not box.exec():
                event.ignore()
                return
            self.worker.stop()
            self.worker.wait(5000)
        try:
            self.collect_config().save()
        except Exception:
            pass
        event.accept()


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
