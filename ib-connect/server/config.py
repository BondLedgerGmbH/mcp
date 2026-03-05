"""Configuration management for ib-connect MCP server."""

import json
import os
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ib-connect")

DEFAULT_CONFIG = {
    "gateway_jar_path": "~/.ib-connect/gateway",
    "cache_dir": "~/.ib-connect/cache",
    "log_dir": "~/.ib-connect/logs",
    "java_path": "/opt/homebrew/opt/openjdk/bin/java",
    "browser_command": "open",
    "cache_ttl_minutes": 60,
    "api_call_delay_ms": 300,
    "concentration_thresholds": {
        "single_position_pct": 10.0,
        "sector_pct": 30.0
    },
    "last_update_check": None,
    "update_available": False,
    "rollback_active": False,
    "rollback_reason": None,
    "accounts": {
        "main": {
            "port": 5100,
            "account_id": "",
            "label": "Main Account",
            "type": "individual",
            "tax_treatment": "no_capital_gains_tax"
        }
    }
}

CONFIG_PATH = Path("~/.ib-connect/config.json").expanduser()


def _expand_paths(cfg: dict) -> dict:
    """Expand ~ in path fields."""
    for key in ("gateway_jar_path", "cache_dir", "log_dir"):
        if key in cfg and isinstance(cfg[key], str):
            cfg[key] = str(Path(cfg[key]).expanduser())
    return cfg


def load_config() -> dict:
    """Load config from disk, creating default if not found."""
    if not CONFIG_PATH.exists():
        logger.info("No config found, creating default at %s", CONFIG_PATH)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        cfg = DEFAULT_CONFIG.copy()
    else:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)

    # Merge any missing keys from defaults
    for key, val in DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = val

    return _expand_paths(cfg)


def save_config(cfg: dict):
    """Write config to disk, preserving file permissions."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Preserve permissions if file exists
    existing_mode = None
    if CONFIG_PATH.exists():
        existing_mode = CONFIG_PATH.stat().st_mode

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, default=str)

    if existing_mode is not None:
        os.chmod(CONFIG_PATH, existing_mode)
    else:
        os.chmod(CONFIG_PATH, 0o600)


def update_config_field(field: str, value):
    """Update a single field in the config and save."""
    cfg = load_config()
    cfg[field] = value
    save_config(cfg)


def get_account_config(cfg: dict, account_name: str) -> dict:
    """Get config for a specific account."""
    accounts = cfg.get("accounts", {})
    if account_name not in accounts:
        raise ValueError(f"Unknown account: {account_name}. Available: {list(accounts.keys())}")
    return accounts[account_name]


def get_all_account_names(cfg: dict) -> list:
    """Get list of all configured account names."""
    return list(cfg.get("accounts", {}).keys())
