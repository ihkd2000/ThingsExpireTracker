"""Signed links for the "I'm on it" button in reminder emails. They need no login, so they expire."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

from .config import Settings

SNOOZE_DAYS = 7
LINK_LIFETIME = 60 * 24 * 3600


def _secret(settings: Settings) -> str:
    return settings.secret or settings.api_token


def enabled(settings: Settings) -> bool:
    return bool(settings.public_url and _secret(settings))


def _signature(settings: Settings, item_id: int, days: int, expires: int, expires_on: str) -> str:
    message = f"snooze:{item_id}:{days}:{expires}:{expires_on}".encode()
    return hmac.new(_secret(settings).encode(), message, hashlib.sha256).hexdigest()[:40]


def snooze_link(settings: Settings, item: dict[str, Any], days: int = SNOOZE_DAYS, now: Optional[float] = None) -> str:
    """Bound to the item's current expiry date, so the link stops working once the item is renewed."""
    expires = int((now if now is not None else time.time()) + LINK_LIFETIME)
    query = urlencode({"i": item["id"], "d": days, "x": expires, "s": _signature(settings, item["id"], days, expires, item["expires_on"])})
    return f"{settings.public_url}/ack?{query}"


def verify(settings: Settings, item: dict[str, Any], days: int, expires: int, signature: str, now: Optional[float] = None) -> bool:
    if not _secret(settings) or expires < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(signature, _signature(settings, item["id"], days, expires, item["expires_on"]))
