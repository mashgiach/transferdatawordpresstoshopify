"""Configuration model, persisted as JSON in the user's home directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

APP_DIR = Path(os.environ.get("WOO2SHOPIFY_HOME", Path.home() / ".woo2shopify"))
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.sqlite3"
EXPORT_DIR = APP_DIR / "exports"
LOG_PATH = APP_DIR / "migration.log"

DEFAULT_API_VERSION = "2025-01"

# Woo statuses we import by default. "trash" and "checkout-draft" are skipped.
DEFAULT_ORDER_STATUSES = [
    "completed",
    "processing",
    "on-hold",
    "cancelled",
    "refunded",
    "failed",
    "pending",
]


@dataclass
class WooConfig:
    base_url: str = ""
    consumer_key: str = ""
    consumer_secret: str = ""
    verify_ssl: bool = True
    timeout: int = 60
    # Some hosts strip the Authorization header; then query-string auth is needed.
    query_string_auth: bool = False
    # Optional WordPress application password, used to pull non-customer WP users.
    wp_username: str = ""
    wp_app_password: str = ""

    @property
    def api_root(self) -> str:
        return self.base_url.rstrip("/") + "/wp-json/wc/v3"

    @property
    def wp_api_root(self) -> str:
        return self.base_url.rstrip("/") + "/wp-json/wp/v2"


@dataclass
class ShopifyConfig:
    shop_domain: str = ""          # my-store.myshopify.com
    access_token: str = ""         # Admin API access token (shpat_...)
    api_version: str = DEFAULT_API_VERSION
    location_id: str = ""          # gid://shopify/Location/... (auto-detected if blank)
    order_api: str = "graphql"     # "graphql" or "rest"
    # Only needed when the access token is obtained by OAuth from a
    # Dev/Partner Dashboard app rather than pasted from a store custom app.
    client_id: str = ""
    client_secret: str = ""
    oauth_port: int = 3456
    # "client_credentials": mint (and re-mint) tokens from the app credentials.
    # "token": use access_token as pasted.
    auth_mode: str = "client_credentials"

    @property
    def domain(self) -> str:
        """Accepts a bare handle or a pasted URL; always yields the API host."""
        from .oauth import normalize_shop

        return normalize_shop(self.shop_domain)

    @property
    def graphql_url(self) -> str:
        return f"https://{self.domain}/admin/api/{self.api_version}/graphql.json"

    def rest_url(self, path: str) -> str:
        return f"https://{self.domain}/admin/api/{self.api_version}/{path.lstrip('/')}"


@dataclass
class MigrationOptions:
    # ---- what to migrate -------------------------------------------------
    migrate_customers: bool = True
    migrate_orders: bool = True
    include_guest_customers: bool = True     # build customers out of guest orders
    include_wp_users: bool = False           # pull non-customer WP users too
    import_order_notes: bool = True
    import_fulfillments: bool = True
    import_transactions: bool = True

    # ---- date range ------------------------------------------------------
    years_back: int = 4
    date_from: str = ""   # ISO date, overrides years_back when set
    date_to: str = ""

    order_statuses: List[str] = field(default_factory=lambda: list(DEFAULT_ORDER_STATUSES))

    # ---- behaviour -------------------------------------------------------
    dry_run: bool = False
    resume: bool = True                      # skip records already migrated
    page_size: int = 100                     # Woo page size (max 100)
    window_days: int = 31                    # date-window size for order paging
    max_retries: int = 5
    request_delay: float = 0.0               # extra courtesy delay between writes

    # product / variant matching
    match_variants: bool = True
    match_by: str = "sku"                    # sku | sku_then_title | none
    fallback_custom_line_items: bool = True  # keep line items even without a match

    # customers
    marketing_consent: bool = False          # honour Woo opt-in flag when True
    customer_tag: str = "woo-import"
    store_woo_metafields: bool = True

    # orders
    order_tag: str = "woo-import"
    preserve_order_numbers: bool = False
    inventory_behaviour: str = "BYPASS"      # BYPASS | DECREMENT_IGNORING_POLICY | DECREMENT_OBEYING_POLICY
    send_receipts: bool = False
    default_gateway: str = "manual"
    skip_zero_total: bool = False

    def clone(self) -> "MigrationOptions":
        return MigrationOptions(**asdict(self))


@dataclass
class AppConfig:
    woo: WooConfig = field(default_factory=WooConfig)
    shopify: ShopifyConfig = field(default_factory=ShopifyConfig)
    options: MigrationOptions = field(default_factory=MigrationOptions)

    def to_dict(self) -> Dict[str, Any]:
        return {"woo": asdict(self.woo), "shopify": asdict(self.shopify), "options": asdict(self.options)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        def build(klass, payload):
            valid = {f.name for f in fields(klass)}
            return klass(**{k: v for k, v in (payload or {}).items() if k in valid})

        return cls(
            woo=build(WooConfig, data.get("woo")),
            shopify=build(ShopifyConfig, data.get("shopify")),
            options=build(MigrationOptions, data.get("options")),
        )

    def save(self, path: Path = CONFIG_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)  # the file holds API secrets
        except OSError:
            pass
        return path

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
