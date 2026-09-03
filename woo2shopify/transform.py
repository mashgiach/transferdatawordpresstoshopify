"""Mapping layer: WooCommerce/WordPress records -> Shopify Admin API payloads."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import MigrationOptions

# Woo order status -> Shopify financial status
FINANCIAL_STATUS = {
    "completed": "PAID",
    "processing": "PAID",
    "on-hold": "PENDING",
    "pending": "PENDING",
    "cancelled": "VOIDED",
    "failed": "VOIDED",
    "refunded": "REFUNDED",
    "checkout-draft": "PENDING",
}

PROVINCE_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,3}$")
LOCATION_GID_RE = re.compile(r"^gid://shopify/Location/\d+$")
E164_RE = re.compile(r"^\+[1-9]\d{7,15}$")


# --------------------------------------------------------------------- money
def dec(value: Any) -> Decimal:
    if value in (None, "", False):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money_str(value: Any) -> str:
    return f"{dec(value):.2f}"


def is_valid_location_gid(value: str) -> bool:
    return bool(value and LOCATION_GID_RE.match(value.strip()))


def money_bag(amount: Any, currency: str) -> Dict[str, Any]:
    entry = {"amount": money_str(amount), "currencyCode": currency}
    return {"shopMoney": entry, "presentmentMoney": dict(entry)}


# ------------------------------------------------------------------- helpers
def iso_z(value: Optional[str]) -> Optional[str]:
    """Woo gives naive GMT strings ('2021-04-02T10:11:12'); Shopify wants UTC."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[^\d+]", "", raw)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned if E164_RE.match(cleaned) else ""


def _clean(mapping: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


def _meta_value(meta_data: List[Dict[str, Any]], key: str) -> str:
    for item in meta_data or []:
        if item.get("key") == key:
            value = item.get("value")
            return "" if value is None else str(value)
    return ""


def address_input(addr: Dict[str, Any], graphql: bool = True) -> Dict[str, Any]:
    addr = addr or {}
    if not any(addr.get(k) for k in ("address_1", "city", "postcode", "country", "first_name", "last_name")):
        return {}
    country = (addr.get("country") or "").strip().upper()
    province = (addr.get("state") or "").strip()
    phone = (addr.get("phone") or "").strip()

    if graphql:
        payload = {
            "firstName": addr.get("first_name") or "",
            "lastName": addr.get("last_name") or "",
            "company": addr.get("company") or "",
            "address1": addr.get("address_1") or "",
            "address2": addr.get("address_2") or "",
            "city": addr.get("city") or "",
            "zip": addr.get("postcode") or "",
            "phone": phone,
        }
        if len(country) == 2:
            payload["countryCode"] = country
        if province and PROVINCE_CODE_RE.match(province):
            payload["provinceCode"] = province.upper()
        return _clean(payload)

    payload = {
        "first_name": addr.get("first_name") or "",
        "last_name": addr.get("last_name") or "",
        "company": addr.get("company") or "",
        "address1": addr.get("address_1") or "",
        "address2": addr.get("address_2") or "",
        "city": addr.get("city") or "",
        "zip": addr.get("postcode") or "",
        "phone": phone,
    }
    if len(country) == 2:
        payload["country_code"] = country
    if province:
        if PROVINCE_CODE_RE.match(province):
            payload["province_code"] = province.upper()
        else:
            payload["province"] = province
    return _clean(payload)


# ----------------------------------------------------------------- customers
def customer_from_woo(customer: Dict[str, Any], opts: MigrationOptions) -> Dict[str, Any]:
    """Woo customer record -> Shopify CustomerInput."""
    billing = customer.get("billing") or {}
    shipping = customer.get("shipping") or {}
    email = (customer.get("email") or billing.get("email") or "").strip().lower()

    first = customer.get("first_name") or billing.get("first_name") or ""
    last = customer.get("last_name") or billing.get("last_name") or ""

    tags = [opts.customer_tag, f"woo-customer-{customer.get('id')}"]
    role = customer.get("role")
    if role and role != "customer":
        tags.append(f"woo-role-{role}")

    addresses = []
    for addr in (billing, shipping):
        built = address_input(addr, graphql=True)
        if built and built not in addresses:
            addresses.append(built)

    note_lines = [
        f"Imported from WooCommerce (user #{customer.get('id')})",
        f"Username: {customer.get('username') or '-'}",
        f"Registered: {customer.get('date_created_gmt') or customer.get('date_created') or '-'}",
    ]
    if customer.get("is_paying_customer") is not None:
        note_lines.append(f"Paying customer: {customer.get('is_paying_customer')}")
    if billing.get("company"):
        note_lines.append(f"Company: {billing['company']}")
    if billing.get("phone"):
        note_lines.append(f"Billing phone: {billing['phone']}")

    payload: Dict[str, Any] = {
        "firstName": first,
        "lastName": last,
        "note": "\n".join(note_lines)[:5000],
        "tags": tags,
    }
    if email:
        payload["email"] = email
    phone = normalize_phone(billing.get("phone") or shipping.get("phone") or "")
    if phone:
        payload["phone"] = phone
    if addresses:
        payload["addresses"] = addresses

    if opts.marketing_consent:
        opted_in = str(_meta_value(customer.get("meta_data") or [], "_wc_marketing_opt_in")).lower() in ("1", "yes", "true")
        payload["emailMarketingConsent"] = {
            "marketingState": "SUBSCRIBED" if opted_in else "NOT_SUBSCRIBED",
            "marketingOptInLevel": "SINGLE_OPT_IN",
        }

    if opts.store_woo_metafields:
        payload["metafields"] = _customer_metafields(customer)

    return payload


def _customer_metafields(customer: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = [
        ("woo_id", "number_integer", str(customer.get("id") or 0)),
        ("woo_username", "single_line_text_field", customer.get("username") or ""),
        ("woo_registered_at", "single_line_text_field",
         str(customer.get("date_created_gmt") or customer.get("date_created") or "")),
        ("woo_role", "single_line_text_field", customer.get("role") or ""),
    ]
    return [
        {"namespace": "woo_migration", "key": key, "type": mtype, "value": value}
        for key, mtype, value in entries
        if value not in ("", None)
    ]


def customer_from_guest_order(order: Dict[str, Any], opts: MigrationOptions) -> Dict[str, Any]:
    """Build a customer out of a guest order's billing block."""
    billing = order.get("billing") or {}
    pseudo = {
        "id": 0,
        "email": billing.get("email") or "",
        "first_name": billing.get("first_name") or "",
        "last_name": billing.get("last_name") or "",
        "username": "",
        "role": "guest",
        "date_created_gmt": order.get("date_created_gmt"),
        "billing": billing,
        "shipping": order.get("shipping") or {},
        "meta_data": [],
    }
    payload = customer_from_woo(pseudo, opts)
    payload["tags"] = [opts.customer_tag, "woo-guest"]
    payload["note"] = f"Imported from WooCommerce guest order #{order.get('number') or order.get('id')}"
    if opts.store_woo_metafields:
        payload["metafields"] = [
            {"namespace": "woo_migration", "key": "woo_source", "type": "single_line_text_field",
             "value": f"guest-order-{order.get('id')}"}
        ]
    return payload


def customer_from_wp_user(user: Dict[str, Any], opts: MigrationOptions) -> Dict[str, Any]:
    name = (user.get("name") or "").strip()
    first = user.get("first_name") or (name.split(" ")[0] if name else "")
    last = user.get("last_name") or (" ".join(name.split(" ")[1:]) if " " in name else "")
    roles = user.get("roles") or []
    payload: Dict[str, Any] = {
        "firstName": first,
        "lastName": last,
        "tags": [opts.customer_tag, "wp-user"] + [f"wp-role-{r}" for r in roles],
        "note": f"Imported WordPress user #{user.get('id')} ({user.get('slug') or ''})",
    }
    email = (user.get("email") or "").strip().lower()
    if email:
        payload["email"] = email
    if opts.store_woo_metafields:
        payload["metafields"] = [
            {"namespace": "woo_migration", "key": "woo_id", "type": "number_integer", "value": str(user.get("id") or 0)},
            {"namespace": "woo_migration", "key": "woo_username", "type": "single_line_text_field",
             "value": user.get("slug") or ""},
        ]
    return payload


# -------------------------------------------------------------------- orders
VariantLookup = Callable[[str, str], Optional[Dict[str, str]]]


def financial_status(order: Dict[str, Any]) -> str:
    status = (order.get("status") or "").lower()
    refunded = sum(dec(r.get("total")).copy_abs() for r in (order.get("refunds") or []))
    total = dec(order.get("total"))
    if status == "refunded" or (refunded and total and refunded >= total):
        return "REFUNDED"
    if refunded:
        return "PARTIALLY_REFUNDED"
    return FINANCIAL_STATUS.get(status, "PENDING")


def order_tags(order: Dict[str, Any], opts: MigrationOptions) -> List[str]:
    tags = [opts.order_tag, woo_order_tag(order.get("id"))]
    status = (order.get("status") or "").lower()
    if status:
        tags.append(f"woo-status-{status}")
    return tags


def woo_order_tag(woo_id: Any) -> str:
    return f"woo-order-{woo_id}"


def _line_unit_price(item: Dict[str, Any]) -> Decimal:
    qty = int(item.get("quantity") or 1) or 1
    subtotal = dec(item.get("subtotal"))
    if subtotal:
        return (subtotal / qty).quantize(Decimal("0.0001"))
    return dec(item.get("price"))


def _line_properties(item: Dict[str, Any]) -> List[Dict[str, str]]:
    props = []
    for meta in item.get("meta_data") or []:
        key = str(meta.get("display_key") or meta.get("key") or "")
        if not key or key.startswith("_"):
            continue
        value = meta.get("display_value")
        if value is None:
            value = meta.get("value")
        if isinstance(value, (dict, list)):
            continue
        props.append({"name": key[:255], "value": str(value)[:255]})
    return props[:20]


def build_line_items(
    order: Dict[str, Any],
    opts: MigrationOptions,
    lookup: Optional[VariantLookup],
    currency: str,
    graphql: bool = True,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Returns (line items, warnings)."""
    items: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for item in order.get("line_items") or []:
        qty = int(item.get("quantity") or 1)
        if qty <= 0:
            continue
        sku = (item.get("sku") or "").strip()
        title = item.get("name") or sku or "Imported item"
        unit = _line_unit_price(item)

        match = lookup(sku, title) if (opts.match_variants and lookup) else None
        if not match and not opts.fallback_custom_line_items:
            warnings.append(f"no variant for '{title}' (sku={sku or '-'}) — line skipped")
            continue
        if not match:
            warnings.append(f"no variant for '{title}' (sku={sku or '-'}) — imported as custom line")

        if graphql:
            entry: Dict[str, Any] = {
                "title": title[:255],
                "quantity": qty,
                "priceSet": money_bag(unit, currency),
                "requiresShipping": True,
                "taxable": bool(dec(item.get("total_tax"))),
            }
            if sku:
                entry["sku"] = sku
            if match:
                entry["variantId"] = match["variant_gid"]
            props = _line_properties(item)
            if props:
                entry["properties"] = props
            # Tax is carried at the order level (order.get("tax_lines") below), never here too —
            # Shopify's orderCreate rejects an order that has tax lines on both a line item and
            # the order itself ("must be associated with either order or line item but not both").
        else:
            entry = {
                "title": title[:255],
                "quantity": qty,
                "price": f"{unit:.2f}",
                "requires_shipping": True,
                "taxable": bool(dec(item.get("total_tax"))),
            }
            if sku:
                entry["sku"] = sku
            if match and match.get("variant_id"):
                entry["variant_id"] = int(match["variant_id"])
            props = _line_properties(item)
            if props:
                entry["properties"] = props
            # Same rule for REST: tax stays at the order level only.

        items.append(entry)

    # Woo fees become extra custom line items so the totals reconcile.
    for fee in order.get("fee_lines") or []:
        amount = dec(fee.get("total"))
        if not amount:
            continue
        name = fee.get("name") or "Fee"
        if graphql:
            items.append({
                "title": name[:255],
                "quantity": 1,
                "priceSet": money_bag(amount, currency),
                "requiresShipping": False,
                "taxable": False,
            })
        else:
            items.append({
                "title": name[:255], "quantity": 1, "price": money_str(amount),
                "requires_shipping": False, "taxable": False,
            })

    if not items:
        placeholder_total = dec(order.get("total"))
        title = f"WooCommerce order #{order.get('number') or order.get('id')}"
        if graphql:
            items.append({"title": title, "quantity": 1, "priceSet": money_bag(placeholder_total, currency),
                          "requiresShipping": False, "taxable": False})
        else:
            items.append({"title": title, "quantity": 1, "price": money_str(placeholder_total),
                          "requires_shipping": False, "taxable": False})
        warnings.append("order had no importable line items — added a single placeholder line")

    return items, warnings


def _discount_total(order: Dict[str, Any]) -> Decimal:
    return dec(order.get("discount_total"))


def _note_attribute_pairs(order: Dict[str, Any]) -> List[Tuple[str, str]]:
    attrs = [
        ("woo_order_id", str(order.get("id") or "")),
        ("woo_order_number", str(order.get("number") or "")),
        ("woo_status", str(order.get("status") or "")),
        ("woo_order_key", str(order.get("order_key") or "")),
        ("woo_payment_method", str(order.get("payment_method_title") or order.get("payment_method") or "")),
        ("woo_created_gmt", str(order.get("date_created_gmt") or "")),
        ("woo_customer_ip", str(order.get("customer_ip_address") or "")),
    ]
    refunded = sum(dec(r.get("total")).copy_abs() for r in (order.get("refunds") or []))
    if refunded:
        attrs.append(("woo_refunded_total", f"{refunded:.2f}"))
    coupons = ", ".join(c.get("code", "") for c in (order.get("coupon_lines") or []) if c.get("code"))
    if coupons:
        attrs.append(("woo_coupons", coupons))
    return [(k, v[:255]) for k, v in attrs if v]


def _note_attributes_graphql(order: Dict[str, Any]) -> List[Dict[str, str]]:
    # OrderCreateOrderInput.customAttributes is [AttributeInput!], which takes
    # key/value — not name/value like the REST note_attributes field below.
    return [{"key": k, "value": v} for k, v in _note_attribute_pairs(order)]


def _note_attributes_rest(order: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"name": k, "value": v} for k, v in _note_attribute_pairs(order)]


def order_to_graphql(
    order: Dict[str, Any],
    opts: MigrationOptions,
    lookup: Optional[VariantLookup],
    customer_gid: str = "",
    location_gid: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    currency = (order.get("currency") or "USD").upper()
    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}
    line_items, warnings = build_line_items(order, opts, lookup, currency, graphql=True)
    status = (order.get("status") or "").lower()
    fin = financial_status(order)

    payload: Dict[str, Any] = {
        "currency": currency,
        "presentmentCurrency": currency,
        "lineItems": line_items,
        "processedAt": iso_z(order.get("date_created_gmt") or order.get("date_created")),
        "financialStatus": fin,
        "tags": order_tags(order, opts),
        "customAttributes": _note_attributes_graphql(order),
        "taxesIncluded": bool(order.get("prices_include_tax")),
        "sourceName": "woocommerce",
        "test": False,
    }

    email = (billing.get("email") or "").strip().lower()
    if email:
        payload["email"] = email
    phone = normalize_phone(billing.get("phone") or "")
    if phone:
        payload["phone"] = phone
    if customer_gid:
        payload["customer"] = {"toAssociate": {"id": customer_gid}}

    bill = address_input(billing, graphql=True)
    ship = address_input(shipping, graphql=True) or bill
    if bill:
        payload["billingAddress"] = bill
    if ship:
        payload["shippingAddress"] = ship

    note_parts = []
    if order.get("customer_note"):
        note_parts.append(str(order["customer_note"]))
    if note_parts:
        payload["note"] = "\n".join(note_parts)[:5000]

    shipping_lines = []
    for line in order.get("shipping_lines") or []:
        shipping_lines.append(_clean({
            "title": (line.get("method_title") or "Shipping")[:255],
            "code": (line.get("method_id") or "")[:255],
            "priceSet": money_bag(line.get("total"), currency),
            "source": "woocommerce",
        }))
    if shipping_lines:
        payload["shippingLines"] = shipping_lines

    tax_lines = []
    for line in order.get("tax_lines") or []:
        amount = dec(line.get("tax_total")) + dec(line.get("shipping_tax_total"))
        if not amount:
            continue
        entry = {
            "title": (line.get("label") or line.get("rate_code") or "Tax")[:255],
            "priceSet": money_bag(amount, currency),
        }
        rate = dec(line.get("rate_percent"))
        if rate:
            entry["rate"] = float(rate / 100)
        tax_lines.append(entry)
    if tax_lines:
        payload["taxLines"] = tax_lines

    discount = _discount_total(order)
    if discount > 0:
        coupons = [c.get("code") for c in (order.get("coupon_lines") or []) if c.get("code")]
        payload["discountCode"] = {
            "itemFixedDiscountCode": {
                "code": (coupons[0] if coupons else "WOO-DISCOUNT")[:255],
                "amountSet": money_bag(discount, currency),
            }
        }

    if opts.import_transactions and fin in ("PAID",):
        payload["transactions"] = [{
            "kind": "SALE",
            "status": "SUCCESS",
            "amountSet": money_bag(order.get("total"), currency),
            "gateway": (order.get("payment_method") or opts.default_gateway or "manual")[:255],
            "processedAt": iso_z(order.get("date_paid_gmt") or order.get("date_created_gmt")),
        }]

    if opts.import_fulfillments and status == "completed" and is_valid_location_gid(location_gid):
        payload["fulfillment"] = {"locationId": location_gid, "notifyCustomer": False}

    if status in ("cancelled", "failed"):
        payload["closedAt"] = iso_z(order.get("date_modified_gmt") or order.get("date_created_gmt"))

    if opts.preserve_order_numbers and order.get("number"):
        payload["name"] = f"#{order['number']}"

    if opts.store_woo_metafields:
        payload["metafields"] = [
            {"namespace": "woo_migration", "key": "woo_id", "type": "number_integer", "value": str(order.get("id") or 0)},
            {"namespace": "woo_migration", "key": "woo_number", "type": "single_line_text_field",
             "value": str(order.get("number") or "")},
            {"namespace": "woo_migration", "key": "woo_status", "type": "single_line_text_field",
             "value": str(order.get("status") or "")},
        ]

    options = {
        "inventoryBehaviour": opts.inventory_behaviour,
        "sendReceipt": bool(opts.send_receipts),
        "sendFulfillmentReceipt": False,
    }
    return _clean(payload), options, warnings


def order_to_rest(
    order: Dict[str, Any],
    opts: MigrationOptions,
    lookup: Optional[VariantLookup],
    customer_legacy_id: str = "",
) -> Tuple[Dict[str, Any], List[str]]:
    currency = (order.get("currency") or "USD").upper()
    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}
    line_items, warnings = build_line_items(order, opts, lookup, currency, graphql=False)
    status = (order.get("status") or "").lower()
    fin = financial_status(order).lower()

    payload: Dict[str, Any] = {
        "currency": currency,
        "line_items": line_items,
        "financial_status": fin,
        "processed_at": iso_z(order.get("date_created_gmt") or order.get("date_created")),
        "created_at": iso_z(order.get("date_created_gmt") or order.get("date_created")),
        "tags": ", ".join(order_tags(order, opts)),
        "note_attributes": _note_attributes_rest(order),
        "taxes_included": bool(order.get("prices_include_tax")),
        "source_name": "woocommerce",
        "inventory_behaviour": opts.inventory_behaviour.lower(),
        "send_receipt": bool(opts.send_receipts),
        "send_fulfillment_receipt": False,
        "test": False,
    }

    email = (billing.get("email") or "").strip().lower()
    if email:
        payload["email"] = email
    phone = normalize_phone(billing.get("phone") or "")
    if phone:
        payload["phone"] = phone
    if customer_legacy_id:
        payload["customer"] = {"id": int(customer_legacy_id)}

    bill = address_input(billing, graphql=False)
    ship = address_input(shipping, graphql=False) or bill
    if bill:
        payload["billing_address"] = bill
    if ship:
        payload["shipping_address"] = ship
    if order.get("customer_note"):
        payload["note"] = str(order["customer_note"])[:5000]

    shipping_lines = []
    for line in order.get("shipping_lines") or []:
        shipping_lines.append(_clean({
            "title": (line.get("method_title") or "Shipping")[:255],
            "code": (line.get("method_id") or "")[:255],
            "price": money_str(line.get("total")),
            "source": "woocommerce",
        }))
    if shipping_lines:
        payload["shipping_lines"] = shipping_lines

    tax_lines = []
    for line in order.get("tax_lines") or []:
        amount = dec(line.get("tax_total")) + dec(line.get("shipping_tax_total"))
        if not amount:
            continue
        entry = {"title": (line.get("label") or "Tax")[:255], "price": money_str(amount)}
        rate = dec(line.get("rate_percent"))
        if rate:
            entry["rate"] = float(rate / 100)
        tax_lines.append(entry)
    if tax_lines:
        payload["tax_lines"] = tax_lines

    discount = _discount_total(order)
    if discount > 0:
        coupons = [c.get("code") for c in (order.get("coupon_lines") or []) if c.get("code")]
        payload["discount_codes"] = [{
            "code": (coupons[0] if coupons else "WOO-DISCOUNT")[:255],
            "amount": money_str(discount),
            "type": "fixed_amount",
        }]

    if opts.import_transactions and fin == "paid":
        payload["transactions"] = [{
            "kind": "sale",
            "status": "success",
            "amount": money_str(order.get("total")),
            "gateway": (order.get("payment_method") or opts.default_gateway or "manual")[:255],
            "processed_at": iso_z(order.get("date_paid_gmt") or order.get("date_created_gmt")),
        }]

    if opts.import_fulfillments and status == "completed":
        payload["fulfillment_status"] = "fulfilled"
    if status in ("cancelled", "failed"):
        payload["cancelled_at"] = iso_z(order.get("date_modified_gmt") or order.get("date_created_gmt"))
        payload["cancel_reason"] = "other"
    if opts.preserve_order_numbers and order.get("number"):
        payload["name"] = f"#{order['number']}"

    return _clean(payload), warnings


def order_notes_text(notes: List[Dict[str, Any]]) -> str:
    lines = []
    for note in sorted(notes or [], key=lambda n: str(n.get("date_created_gmt") or "")):
        stamp = note.get("date_created_gmt") or note.get("date_created") or ""
        author = note.get("author") or "system"
        kind = "customer" if note.get("customer_note") else "internal"
        lines.append(f"[{stamp}] ({kind}/{author}) {note.get('note', '')}")
    return "\n".join(lines)
