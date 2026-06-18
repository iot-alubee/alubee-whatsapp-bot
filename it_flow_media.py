"""Download, decrypt, and store optional IT issue photos from WhatsApp Flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    AES = None  # type: ignore[misc, assignment]
    unpad = None  # type: ignore[misc, assignment]


def _b64decode(value: str) -> bytes:
    raw = (value or "").strip()
    if not raw:
        return b""
    pad = "=" * (-len(raw) % 4)
    return base64.b64decode(raw + pad)


def _entry_meta(entry: dict) -> dict:
    nested = entry.get("encryption_metadata")
    if isinstance(nested, dict) and nested:
        return nested
    keys = (
        "encryption_key",
        "hmac_key",
        "iv",
        "encrypted_hash",
        "plaintext_hash",
        "hmac",
    )
    flat = {k: entry.get(k) for k in keys if entry.get(k) is not None}
    return flat


def _looks_like_photo_entry(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("cdn_url"):
        return bool(_entry_meta(item))
    if item.get("media_id") and _entry_meta(item):
        return True
    return False


def _photo_entries(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw] if _looks_like_photo_entry(raw) else []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict) and _looks_like_photo_entry(item)]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith(("[", "{")):
            try:
                parsed = json.loads(s)
                return _photo_entries(parsed)
            except json.JSONDecodeError:
                return []
    return []


def deep_find_issue_photo(obj: Any, depth: int = 0) -> Any | None:
    """Search nested flow/webhook JSON for PhotoPicker payload."""
    if depth > 10 or obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("issue_photo", "photo", "issue_photos", "photo_picker"):
            val = obj.get(key)
            if val:
                return val
        if _looks_like_photo_entry(obj):
            return obj
        for value in obj.values():
            found = deep_find_issue_photo(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = deep_find_issue_photo(item, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith(("[", "{")):
            try:
                return deep_find_issue_photo(json.loads(s), depth + 1)
            except json.JSONDecodeError:
                return None
    return None


def photo_debug_summary(raw_photo: Any) -> str:
    if raw_photo is None:
        return "no_photo_in_payload"
    entries = _photo_entries(raw_photo)
    if not entries:
        kind = type(raw_photo).__name__
        if isinstance(raw_photo, str):
            return f"unparsed_photo_string len={len(raw_photo.strip())}"
        if isinstance(raw_photo, dict):
            return f"photo_dict_keys={','.join(sorted(raw_photo.keys()))}"
        return f"photo_present_type={kind}_no_entries"
    entry = entries[0]
    has_cdn = bool((entry.get("cdn_url") or "").strip())
    has_meta = bool(_entry_meta(entry))
    return f"entries=1 cdn_url={has_cdn} meta={has_meta}"


def decrypt_flow_media_file(cdn_bytes: bytes, meta: dict) -> bytes:
    """Decrypt WhatsApp Flow CDN media per Meta media_upload reference."""
    if AES is None or unpad is None:
        raise RuntimeError("pycryptodome is required for IT photo decryption")

    encrypted_hash = _b64decode(str(meta.get("encrypted_hash") or ""))
    if encrypted_hash:
        digest = hashlib.sha256(cdn_bytes).digest()
        if not hmac.compare_digest(digest, encrypted_hash):
            raise ValueError("Encrypted file hash mismatch")

    if len(cdn_bytes) < 11:
        raise ValueError("Encrypted media file too short")

    ciphertext = cdn_bytes[:-10]
    hmac10 = cdn_bytes[-10:]
    encryption_key = _b64decode(str(meta.get("encryption_key") or ""))
    hmac_key = _b64decode(str(meta.get("hmac_key") or ""))
    iv = _b64decode(str(meta.get("iv") or ""))
    plaintext_hash = _b64decode(str(meta.get("plaintext_hash") or ""))

    calc_hmac = hmac.new(hmac_key, iv + ciphertext, hashlib.sha256).digest()[:10]
    if not hmac.compare_digest(calc_hmac, hmac10):
        raise ValueError("HMAC validation failed")

    cipher = AES.new(encryption_key, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)

    if plaintext_hash:
        digest = hashlib.sha256(plaintext).digest()
        if not hmac.compare_digest(digest, plaintext_hash):
            raise ValueError("Plaintext hash mismatch")

    return plaintext


def _safe_filename(name: str, fallback: str = "issue.jpg") -> str:
    base = os.path.basename((name or "").strip()) or fallback
    base = re.sub(r"[^\w.\-]+", "_", base)
    return base[:120] or fallback


def _content_type_for_name(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "image/jpeg"


def _upload_to_firebase_storage(
    request_id: str,
    file_name: str,
    content: bytes,
    content_type: str,
) -> tuple[str, str]:
    from firebase_admin import storage

    bucket_name = (os.getenv("FIREBASE_STORAGE_BUCKET") or "").strip()
    if not bucket_name:
        project = (os.getenv("FIREBASE_PROJECT_ID") or "whatsapp-approval-system").strip()
        bucket_name = f"{project}.appspot.com"

    blob_path = f"it-requests/{request_id}/{file_name}"
    bucket = storage.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(content, content_type=content_type)
    try:
        blob.make_public()
        url = blob.public_url
    except Exception:
        logger.warning("make_public failed for %s; using signed URL", blob_path)
        url = blob.generate_signed_url(expiration=60 * 60 * 24 * 365)
    return url, blob_path


def process_it_issue_photo(raw_photo: Any, request_id: str) -> tuple[dict[str, str] | None, str]:
    """
    Download + decrypt one optional Flow photo and persist to Firebase Storage.
    Returns (Firestore fields or None, status message for debugging).
    """
    entries = _photo_entries(raw_photo)
    if not entries:
        return None, photo_debug_summary(raw_photo)

    entry = entries[0]
    cdn_url = (entry.get("cdn_url") or "").strip()
    meta = _entry_meta(entry)
    if not cdn_url:
        return None, "photo_missing_cdn_url"
    if not meta:
        return None, "photo_missing_encryption_metadata"

    file_name = _safe_filename(str(entry.get("file_name") or "issue.jpg"))
    try:
        resp = requests.get(cdn_url, timeout=60)
        resp.raise_for_status()
        plaintext = decrypt_flow_media_file(resp.content, meta)
        content_type = _content_type_for_name(file_name)
        url, path = _upload_to_firebase_storage(
            request_id, file_name, plaintext, content_type
        )
        logger.info(
            "IT issue photo uploaded request_id=%s path=%s bytes=%s",
            request_id,
            path,
            len(plaintext),
        )
        return {
            "issue_photo_url": url,
            "issue_photo_path": path,
            "issue_photo_file_name": file_name,
            "issue_photo_status": "uploaded",
        }, "uploaded"
    except Exception as exc:
        logger.exception("IT issue photo processing failed request_id=%s", request_id)
        return None, f"upload_failed:{type(exc).__name__}:{exc}"
