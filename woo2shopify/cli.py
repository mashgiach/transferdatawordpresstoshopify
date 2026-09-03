"""Headless entry point: `python -m woo2shopify.cli ...`"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import AppConfig, CONFIG_PATH, STATE_PATH, ensure_dirs
from .migrator import Control, Migrator, Reporter
from .oauth import DEFAULT_SCOPES, fetch_offline_token, redirect_uri

COLOURS = {"info": "", "success": "\033[32m", "warn": "\033[33m", "error": "\033[31m"}
RESET = "\033[0m"


def make_reporter(quiet: bool = False) -> Reporter:
    state = {"phase": "", "last": -1}

    def log(message: str, level: str = "info") -> None:
        colour = COLOURS.get(level, "")
        print(f"{colour}[{level}] {message}{RESET if colour else ''}", flush=True)

    def progress(phase: str, done: int, total: int) -> None:
        if quiet:
            return
        if done == state["last"] and phase == state["phase"]:
            return
        state.update(phase=phase, last=done)
        if done % 25 == 0 or (total and done == total):
            suffix = f"/{total}" if total else ""
            print(f"  {phase}: {done}{suffix}", flush=True)

    return Reporter(on_log=log, on_progress=progress)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="woo2shopify",
        description="Migrate WooCommerce customers and orders into Shopify.",
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="config JSON (default: %(default)s)")
    parser.add_argument("--state", default=str(STATE_PATH), help="sqlite state file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write a blank config file to fill in")
    oauth = sub.add_parser("oauth", help="exchange an app's Client ID/secret for an Admin API token")
    oauth.add_argument("--scopes", default=DEFAULT_SCOPES)
    oauth.add_argument("--port", type=int, help="local callback port (default from config)")
    oauth.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    sub.add_parser("test", help="check both API connections")
    sub.add_parser("variants", help="(re)build the Shopify SKU -> variant map")
    sub.add_parser("customers", help="migrate customers only")
    sub.add_parser("orders", help="migrate orders only")
    sub.add_parser("report", help="export CSV reports")
    run = sub.add_parser("run", help="full migration (customers then orders)")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--years", type=int, help="how many years of orders to import")
    run.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD")
    run.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD")
    run.add_argument("--no-customers", action="store_true")
    run.add_argument("--no-orders", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ensure_dirs()
    config_path = Path(args.config)

    if args.command == "init":
        if config_path.exists():
            print(f"{config_path} already exists — leaving it alone.")
            return 0
        AppConfig().save(config_path)
        print(f"Wrote {config_path}. Fill in the Woo and Shopify credentials.")
        return 0

    config = AppConfig.load(config_path)
    for key, attr in (("years", "years_back"), ("date_from", "date_from"), ("date_to", "date_to")):
        value = getattr(args, key, None)
        if value:
            setattr(config.options, attr, value)
    if getattr(args, "dry_run", False):
        config.options.dry_run = True
    if getattr(args, "no_customers", False):
        config.options.migrate_customers = False
    if getattr(args, "no_orders", False):
        config.options.migrate_orders = False

    reporter = make_reporter()
    migrator = Migrator(config, reporter=reporter, control=Control(), state_path=Path(args.state))
    try:
        if args.command == "oauth":
            if args.port:
                config.shopify.oauth_port = args.port
            print(f"Register this redirect URL on the app first: {redirect_uri(config.shopify.oauth_port)}")
            result = fetch_offline_token(
                config.shopify.shop_domain,
                config.shopify.client_id,
                config.shopify.client_secret,
                scopes=args.scopes,
                port=config.shopify.oauth_port,
                open_browser=not args.no_browser,
                log=lambda message, level="info": print(f"[{level}] {message}"),
            )
            config.shopify.access_token = result["access_token"]
            config.save(config_path)
            print(f"Access token saved to {config_path} (scopes: {result['scope']})")
        elif args.command == "test":
            shop = migrator.test_shopify()
            print(f"Shopify OK: {shop.get('name')} ({shop.get('myshopifyDomain')})")
            woo = migrator.test_woo()
            print(f"WooCommerce OK: {woo['orders']} orders, {woo['customers']} customers")
        elif args.command == "variants":
            migrator.build_variant_map()
        elif args.command == "customers":
            migrator.migrate_customers()
        elif args.command == "orders":
            migrator.migrate_orders()
        elif args.command == "report":
            for path in migrator.export_reports():
                print(path)
        elif args.command == "run":
            stats = migrator.run()
            print("\nSummary:")
            for key, value in stats.items():
                print(f"  {key:20} {value}")
    except KeyboardInterrupt:
        print("\nInterrupted — rerun the same command to resume.")
        return 130
    except Exception as exc:  # surfaced instead of a bare traceback
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    finally:
        migrator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
