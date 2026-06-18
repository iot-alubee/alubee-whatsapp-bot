"""Download, decrypt, and store optional IT issue photos from WhatsApp Flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
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


def _photo_entries(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw] if raw.get("cdn_url") or raw.get("encryption_metadata") else []
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict) and (item.get("cdn_url") or item.get("encryption_metadata")):
                out.append(item)
        return out
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                import json

                parsed = json.loads(s)
                return _photo_entries(parsed)
            except json.JSONDecodeError:
                return []
    return []


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


def process_it_issue_photo(raw_photo: Any, request_id: str) -> dict[str, str] | None:
    """
    Download + decrypt one optional Flow photo and persist to Firebase Storage.
    Returns Firestore fields or None when no photo / processing failed.
    """
    entries = _photo_entries(raw_photo)
    if not entries:
        return None

    entry = entries[0]
    cdn_url = (entry.get("cdn_url") or "").strip()
    meta = entry.get("encryption_metadata") or {}
    if not cdn_url or not isinstance(meta, dict):
        logger.warning("IT photo missing cdn_url or encryption_metadata request_id=%s", request_id)
        return None

    file_name = _safe_filename(str(entry.get("file_name") or "issue.jpg"))
    try:
        resp = requests.get(cdn_url, timeout=60)
        resp.raise_for_status()
        plaintext = decrypt_flow_media_file(resp.content, meta)
        content_type = _content_type_for_name(file_name)
        url, path = _upload_to_firebase_storage(
            request_id, file_name, plaintext, content_type
        )
        return {
            "issue_photo_url": url,
            "issue_photo_path": path,
            "issue_photo_file_name": file_name,
        }
    except Exception:
        logger.exception("IT issue photo processing failed request_id=%s", request_id)
        return None
