"""Opaque, scope-bound cursors; callers pin an immutable revision."""
import base64
import hashlib
import hmac
import json

from .contracts import LabError
from .storage import canonical


def request_digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def encode_cursor(scope: dict, offset: int, key: bytes) -> str:
    raw = canonical({"scope": request_digest(scope), "offset": offset})
    return base64.urlsafe_b64encode(raw + hmac.digest(key, raw, "sha256")).decode().rstrip("=")


def decode_cursor(cursor: str | None, scope: dict, key: bytes) -> int:
    if cursor is None:
        return 0
    try:
        if len(cursor) > 2048:
            raise ValueError()
        value = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        raw, signature = value[:-32], value[-32:]
        record = json.loads(raw)
        if (not hmac.compare_digest(signature, hmac.digest(key, raw, "sha256"))
                or record["scope"] != request_digest(scope) or type(record["offset"]) is not int
                or record["offset"] < 0):
            raise ValueError()
        return record["offset"]
    except (ValueError, KeyError, TypeError):
        raise LabError("STALE_INPUT", "Cursor does not match this snapshot, revision, or query.") from None
