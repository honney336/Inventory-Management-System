"""Outbound notifications for stock events.

Delivery must never break the request that triggered it, so every notifier
swallows its own errors and records them in the log.
"""

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import current_app

from .services import get_setting

logger = logging.getLogger(__name__)

EVENT_LOW_STOCK = "stock.low"
EVENT_OUT_OF_STOCK = "stock.out_of_stock"
EVENT_BATCH_EXPIRING = "stock.batch_expiring"
EVENT_ADJUSTMENT = "stock.adjusted"

ALLOWED_SCHEMES = ("http", "https")


class UnsafeURLError(ValueError):
    pass


def _is_public(host):
    addresses = []
    try:
        infos = socket.getaddrinfo(host, None)
        addresses = {info[4][0] for info in infos}
    except socket.gaierror:
        raise UnsafeURLError(f"cannot resolve {host}") from None
    if not addresses:
        raise UnsafeURLError(f"cannot resolve {host}")

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def validate_webhook_url(url, allow_private=None):
    """Accept only public http(s) endpoints unless private ones are allowed."""
    url = (url or "").strip()
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError("only http:// and https:// are allowed")
    if not parsed.hostname:
        raise UnsafeURLError("the URL has no host")
    if parsed.username or parsed.password:
        raise UnsafeURLError("credentials in the URL are not allowed")

    if allow_private is None:
        allow_private = bool(
            current_app.config.get("ALLOW_PRIVATE_WEBHOOKS", False)
        ) if current_app else False
    if not allow_private and not _is_public(parsed.hostname):
        raise UnsafeURLError("the host resolves to a private or reserved address")
    return url


def send_webhook(url, payload, timeout=5):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "inventory-notifier/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def dispatch(event, **payload):
    """Fan an event out to the configured channels. Returns the payload sent."""
    body = {
        "event": event,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "site": get_setting("company_name", "Inventory System"),
        "data": payload,
    }

    logger.info("event %s %s", event, payload)

    url = (get_setting("notify_webhook_url", "") or "").strip()
    if not url:
        return body

    try:
        send_webhook(validate_webhook_url(url), body)
    except UnsafeURLError as exc:
        logger.error("webhook rejected: %s", exc)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.error("webhook delivery failed: %s", exc)
    return body


def check_stock_levels(products):
    """Fire low-stock and out-of-stock events for the given products."""
    for product in products:
        status = product.stock_status
        if status == "out":
            dispatch(EVENT_OUT_OF_STOCK, sku=product.sku, name=product.name,
                     on_hand=product.quantity, reorder_point=product.reorder_point)
        elif status == "low":
            dispatch(
                EVENT_LOW_STOCK,
                sku=product.sku,
                name=product.name,
                on_hand=product.quantity,
                reorder_point=product.reorder_point,
                suggested_order_qty=product.suggested_order_qty,
                supplier=product.supplier.name if product.supplier else None,
            )


def check_expiry(days=None):
    from .services import batch_summary

    summary = batch_summary(days)
    for batch in summary["expired"]:
        dispatch(
            EVENT_BATCH_EXPIRING,
            state="expired",
            sku=batch.product.sku if batch.product else None,
            batch_no=batch.batch_no,
            quantity=batch.quantity,
            expiry_date=batch.expiry_date.isoformat() if batch.expiry_date else None,
            value=round(batch.value, 2),
        )
    for batch in summary["expiring"]:
        dispatch(
            EVENT_BATCH_EXPIRING,
            state="expiring",
            sku=batch.product.sku if batch.product else None,
            batch_no=batch.batch_no,
            quantity=batch.quantity,
            expiry_date=batch.expiry_date.isoformat() if batch.expiry_date else None,
            days_left=batch.days_to_expiry,
            value=round(batch.value, 2),
        )
    return summary