# WooCommerce → Shopify migration (customers & orders)

Moves **customers, WordPress users and order history** out of WooCommerce into Shopify.
Products are assumed to already be in Shopify — order line items are re-linked to the
existing Shopify variants by SKU.

Ships with a PyQt5 / [PyQt-Fluent-Widgets](https://pyqt-fluent-widgets.readthedocs.io/en/latest/)
desktop UI and an equivalent command line interface.

![pages](docs/screenshot-run.png)

## What gets migrated

| WooCommerce | Shopify |
|---|---|
| Customers (`/wc/v3/customers`) | Customers, with billing + shipping address, phone, tags, note |
| Guest-order buyers | Customers built from the order's billing block |
| WordPress users with no orders (optional) | Customers tagged `wp-user`, `wp-role-<role>` |
| Orders, any status, any date range | Orders created with their original date (`processedAt`) |
| Line items | Matched to Shopify variants by SKU; unmatched lines are kept as custom lines so totals stay correct |
| Fees | Extra custom line items |
| Shipping lines | `shippingLines` |
| Tax lines + "prices include tax" | `taxLines` + `taxesIncluded` |
| Coupons / `discount_total` | A fixed-amount order discount named after the first coupon |
| Payment method + paid date | A successful `SALE` transaction |
| `completed` status | Order marked fulfilled at your primary location |
| `refunded` / partial refunds | `REFUNDED` / `PARTIALLY_REFUNDED` financial status, refunded amount kept in the order attributes |
| Order notes | Appended to the Shopify order note |
| Woo ids, order key, status, IP, coupons | Order custom attributes + `woo_migration` metafields |

Every imported order is tagged `woo-order-<id>` and every customer `woo-customer-<id>`,
so the import is traceable and re-runnable.

## Requirements

* Python 3.9+
* A WooCommerce REST API key (read access is enough):
  *WooCommerce → Settings → Advanced → REST API → Add key*
* A Shopify app (Dev Dashboard) installed on the store, with the scopes
  `write_customers`, `write_orders`, `read_products`, `read_locations` — see
  *Getting the Shopify token* below
* Optional, only to import users who never ordered: a WordPress
  **application password** (*Users → Profile → Application Passwords*)

### Getting the Shopify token

Shopify no longer lets you create admin-made custom apps, and an app built in the
**Dev Dashboard never displays an Admin API token** — it only shows a Client ID and a
Client secret. Pasting either of those into the token field gives
`401 [API] Invalid API key or access token`. They have to be exchanged for a token.

**Your app is installed on your own store (same Shopify organization) — use the client
credentials grant.** No browser, no redirect URL:

1. Confirm the app version declares the four scopes above and has been released, and that
   the app is installed on the store.
2. On the Connections page enter the shop domain (`my-store.myshopify.com`), the Client ID
   and the Client secret, leave **Auth mode** on `client_credentials`, and press
   **Mint token**. Headless equivalent: `python -m woo2shopify.cli token`.

These tokens expire after **24 hours**. That is shorter than a large import, so with
`client_credentials` the tool holds on to the Client ID/secret and re-mints the token
automatically — before expiry, and again if Shopify ever returns a 401 mid-run. A
migration spanning days needs no babysitting.

**The store is outside the app's organization** (a client's shop, a distributed app) —
the client credentials grant is refused there. Use **Browser OAuth instead**:

1. Add `http://localhost:3456/callback` to the app's allowed redirect URLs.
2. Press **Browser OAuth instead** (CLI: `python -m woo2shopify.cli oauth`) and approve
   the install. That returns an offline token which does not expire; set Auth mode to
   `token` to use it as-is.

Either way the secret only ever travels to `https://<your-shop>.myshopify.com`.

**Credentials that are not Admin API tokens.** Shopify rejects all of these with the same
opaque 401, so the tool names them instead of letting you guess:

| Starts with | What it actually is |
|---|---|
| `atkn_` | An **App Automation Token** — authenticates Shopify CLI for deploying app versions in CI/CD. It cannot call the Admin API at all. |
| `shpss_` | The app's **Client secret** — exchange it for a token, don't use it as one. |
| `shpca_` | A Customer Account API token. |
| `shppa_` / `prtapi_` | A Partner API token. |

A valid token from the client credentials grant may look like plain hex with no prefix, so
absence of `shpat_` is not itself a problem.

The shop domain field accepts a bare handle (`xzpcy1-7w`), the full myshopify host, or a
pasted URL. It is not the `admin.shopify.com/store/...` address — for
`admin.shopify.com/store/xzpcy1-7w` the domain is `xzpcy1-7w.myshopify.com`.

> Shopify limits how far back apps may create orders. If `orderCreate` rejects old
> orders on your store, ask Shopify support to enable historical order import for the
> app, or switch the **Order API** setting to `rest`.

## Install

```bash
git clone <this repo>
cd transferdatawordpresstoshopify
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run the UI

```bash
python run_ui.py
```

1. **Connections** – Woo store URL + key/secret, Shopify domain + Admin token. Hit
   *Test WooCommerce* and *Test Shopify*; the Shopify test also fills in your location id.
2. **Options** – how many years of orders (default 4), which statuses, what to include.
   Leave **Dry run** on for the first pass.
3. **Product map** – *Build / refresh index* pulls every Shopify variant's SKU. Do this
   before importing orders, otherwise nothing links to your products.
4. **Run** – *Start full migration*. Pause/stop any time; the run resumes where it stopped.
5. **Reports** – summary, failure list, CSV export.

## Run headless

```bash
python -m woo2shopify.cli init                  # write ~/.woo2shopify/config.json
python -m woo2shopify.cli token                 # mint an Admin API token from the app credentials
python -m woo2shopify.cli test                  # check both connections
python -m woo2shopify.cli variants              # build the SKU -> variant index
python -m woo2shopify.cli run --dry-run         # rehearse
python -m woo2shopify.cli run --years 4         # go
python -m woo2shopify.cli run --from 2019-01-01 # or an explicit range
python -m woo2shopify.cli report                # CSVs in ~/.woo2shopify/exports
```

`customers` and `orders` are also available as standalone subcommands.

## How it stays safe to re-run

* Every customer and order is checkpointed in `~/.woo2shopify/state.sqlite3`
  (id, Shopify gid, status, error). A second run skips what already succeeded.
* Before creating an order the tool also searches Shopify for `tag:woo-order-<id>`,
  so a fresh state file still won't produce duplicates.
* Customers are deduplicated by email — an "email has already been taken" error is
  resolved by looking the existing customer up and reusing it.
* Inventory behaviour defaults to `BYPASS` and receipts are off, so importing history
  does not move stock levels or email your customers.

## Suggested order of work

1. Products first (you have done this).
2. `variants` — build the SKU index.
3. `run --dry-run` over a short range, read the log for `no variant for …` warnings and
   fix SKUs on the Shopify side.
4. Customers, then orders, oldest to newest.
5. Check the Reports page, re-run to retry failures.

## Known limits

* Refunds are recorded as a financial status plus the refunded amount in the order
  attributes — individual refund transactions and restocks are not recreated.
* Subscriptions, memberships and other Woo plugin data are out of scope.
* Order numbers are Shopify's own unless *Keep the WooCommerce order number* is on;
  the Woo number is always stored in the order attributes and metafields either way.
* Non-E.164 phone numbers are dropped from the customer record (kept on the address).

## Tests

```bash
python -m unittest discover -s tests -t .
```

Runs a full migration against a local mock of both APIs — customer creation, guest
orders, SKU matching, taxes, discounts, refunds, dry run, resume, and the REST fallback —
plus both token flows, the 24-hour token refresh and the 401 retry
(`tests.test_oauth`).
