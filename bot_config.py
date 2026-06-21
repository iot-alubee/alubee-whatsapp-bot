"""
Load non-secret bot settings from bot_config.env (committed / baked into the image).

Secrets and tokens stay in Cloud Run env or local Interakt/.env only — never in bot_config.env.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

# Never load these from bot_config.env (Cloud Run / .env only).
SECRET_ENV_KEYS = frozenset({
    "INTERAKT_API_KEY",
    "WHATSAPP_CLOUD_API_TOKEN",
    "META_WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_ACCESS_TOKEN",
    "GRAPH_API_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "FIREBASE_CREDENTIALS_JSON",
    "FIREBASE_CREDENTIALS_PATH",
    "FLOW_PRIVATE_KEY_PATH",
})

_BOOTSTRAPPED = False


def _config_path(app_dir: Path) -> Path:
    override = (os.getenv("BOT_CONFIG_PATH") or "").strip()
    if override:
        return Path(override)
    return app_dir / "bot_config.env"


def load_bot_config(app_dir: Path | None = None) -> Path | None:
    """Apply bot_config.env to os.environ (non-secrets only)."""
    base = app_dir or Path(__file__).resolve().parent
    path = _config_path(base)
    if not path.is_file():
        logger.warning("bot_config.env not found at %s", path)
        return None

    preserved = {k: os.environ[k] for k in SECRET_ENV_KEYS if os.environ.get(k)}
    values = dotenv_values(path)
    applied = 0
    for key, value in values.items():
        if not key or value is None:
            continue
        if key in SECRET_ENV_KEYS:
            logger.warning("Ignoring secret key %s in %s — use Cloud Run or .env", key, path.name)
            continue
        os.environ[key] = str(value).strip()
        applied += 1
    for key, value in preserved.items():
        os.environ[key] = value

    logger.info("Loaded %s (%s keys)", path.name, applied)
    return path


def bootstrap_env(app_dir: Path | None = None) -> None:
    """bot_config.env (settings) then optional .env (local secrets). Idempotent."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    base = app_dir or Path(__file__).resolve().parent
    load_bot_config(base)

    env_file = base / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=True)
        logger.info("Loaded local secrets from .env")
    elif not any(os.environ.get(k) for k in SECRET_ENV_KEYS):
        example = base / ".env.example"
        if example.is_file():
            load_dotenv(example, override=True)
            logger.warning(
                "No .env — loaded .env.example for local dev. Copy to .env for real secrets."
            )
