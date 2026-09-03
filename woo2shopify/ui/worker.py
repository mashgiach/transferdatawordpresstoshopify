"""Background threads so the Fluent UI never blocks on the network."""

from __future__ import annotations

import traceback
from typing import Any, Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from ..config import AppConfig
from ..migrator import Control, Migrator, Reporter
from ..oauth import fetch_offline_token


class MigrationWorker(QThread):
    sig_log = pyqtSignal(str, str)
    sig_progress = pyqtSignal(str, int, int)
    sig_stats = pyqtSignal(dict)
    sig_record = pyqtSignal(dict)
    sig_finished = pyqtSignal(str, bool)   # task, ok

    def __init__(self, config: AppConfig, task: str = "run", parent=None):
        super().__init__(parent)
        self.config = config
        self.task = task
        self.control = Control()
        self._migrator: Optional[Migrator] = None

    def stop(self) -> None:
        self.control.stop()

    def pause(self) -> None:
        self.control.pause()

    def resume(self) -> None:
        self.control.resume()

    def run(self) -> None:  # executed in the worker thread
        reporter = Reporter(
            on_log=lambda msg, level="info": self.sig_log.emit(msg, level),
            on_progress=lambda phase, done, total: self.sig_progress.emit(phase, done, total),
            on_stats=lambda stats: self.sig_stats.emit(dict(stats)),
            on_record=lambda row: self.sig_record.emit(dict(row)),
        )
        ok = True
        migrator = None
        try:
            migrator = Migrator(self.config, reporter=reporter, control=self.control)
            self._migrator = migrator
            if self.task == "run":
                migrator.run()
            elif self.task == "customers":
                migrator.migrate_customers()
            elif self.task == "orders":
                migrator.migrate_orders()
            elif self.task == "variants":
                migrator.build_variant_map()
            else:
                raise ValueError(f"unknown task: {self.task}")
        except Exception as exc:
            ok = False
            self.sig_log.emit(f"{type(exc).__name__}: {exc}", "error")
            self.sig_log.emit(traceback.format_exc(limit=4), "error")
        finally:
            if migrator is not None:
                try:
                    migrator.close()
                except Exception:
                    pass
            self.sig_finished.emit(self.task, ok)


class ConnectionTestWorker(QThread):
    sig_result = pyqtSignal(str, bool, str, dict)  # target, ok, message, payload

    def __init__(self, config: AppConfig, target: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.target = target

    def run(self) -> None:
        migrator = None
        payload: Dict[str, Any] = {}
        try:
            migrator = Migrator(self.config, reporter=Reporter(), control=Control())
            if self.target == "woo":
                payload = migrator.test_woo()
                message = (f"WooCommerce reachable — {payload['customers']} customers, "
                           f"{payload['orders']} orders visible.")
            else:
                payload = migrator.test_shopify()
                message = (f"{payload.get('name', 'Store')} ({payload.get('myshopifyDomain', '')}) — "
                           f"currency {payload.get('currencyCode', '?')}, "
                           f"plan {(payload.get('plan') or {}).get('displayName', '?')}")
            self.sig_result.emit(self.target, True, message, payload)
        except Exception as exc:
            self.sig_result.emit(self.target, False, f"{type(exc).__name__}: {exc}", payload)
        finally:
            if migrator is not None:
                try:
                    migrator.close()
                except Exception:
                    pass


class OAuthWorker(QThread):
    """Runs the browser-based token exchange without freezing the window."""

    sig_log = pyqtSignal(str, str)
    sig_result = pyqtSignal(bool, str, str)  # ok, token, message

    def __init__(self, config: AppConfig, scopes: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.scopes = scopes

    def run(self) -> None:
        try:
            result = fetch_offline_token(
                self.config.shopify.shop_domain,
                self.config.shopify.client_id,
                self.config.shopify.client_secret,
                scopes=self.scopes,
                port=self.config.shopify.oauth_port,
                log=lambda message, level="info": self.sig_log.emit(message, level),
            )
            self.sig_result.emit(True, result["access_token"], f"Token granted for {result['scope']}")
        except Exception as exc:
            self.sig_result.emit(False, "", f"{type(exc).__name__}: {exc}")
