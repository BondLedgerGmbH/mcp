"""Dynamic correlation cluster analysis via Claude API.

Detects portfolio changes and evaluates correlated risk clusters using
Claude Haiku. Results are cached alongside portfolio data.

Cluster evaluation triggers when portfolio composition changes (new/removed
positions, significant value shifts). Between changes, cached clusters are
returned.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ib-connect")

_CACHE_DIR = Path.home() / ".ib-connect" / "cache"
_CLUSTER_CACHE = _CACHE_DIR / "clusters_latest.json"

# System prompt — cached across calls (stable prefix)
_SYSTEM_PROMPT = """You are a portfolio risk analyst. Given a list of investment positions, identify groups of positions ("clusters") that would decline together under the same stress scenario.

Rules:
- A cluster must contain 2 or more positions from different asset classes or types
- Each cluster must have a specific, named stress scenario that links the positions
- Positions can appear in multiple clusters
- Only flag clusters where the combined weight exceeds 10% of the portfolio
- Consider cross-asset correlations: e.g., a crypto exchange's stock options correlate with crypto prices; physical gold and gold ETCs move together; multiple positions in the same geographic region share geopolitical risk
- Be specific about WHY positions are correlated, not just that they are in the same sector"""


def _get_api_key() -> str:
    """Retrieve Anthropic API key. Checks: env var → macOS Keychain."""
    import os

    # 1. Environment variable
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key

    # 2. macOS Keychain
    for service_name in ("anthropic-api-key", "ANTHROPIC_API_KEY", "anthropic"):
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service_name, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            key = result.stdout.strip()
            if key and result.returncode == 0:
                return key
        except Exception:
            continue

    raise ValueError(
        "Anthropic API key not found. Set ANTHROPIC_API_KEY env var or store in "
        "macOS Keychain: security add-generic-password -s anthropic-api-key -a $USER -w <key>"
    )


def detect_portfolio_change(old_positions: list[dict], new_positions: list[dict]) -> bool:
    """Detect if portfolio composition changed meaningfully.

    Returns True if:
    - Tickers added or removed
    - Any position value changed by more than 10%
    """
    old_tickers = {p.get("ticker", "") for p in old_positions}
    new_tickers = {p.get("ticker", "") for p in new_positions}

    # New or removed tickers
    if old_tickers != new_tickers:
        logger.info("Portfolio change: tickers differ (added=%s, removed=%s)",
                     new_tickers - old_tickers, old_tickers - new_tickers)
        return True

    # Significant value shifts
    old_values = {p.get("ticker", ""): abs(p.get("base_market_value", 0)) for p in old_positions}
    new_values = {p.get("ticker", ""): abs(p.get("base_market_value", 0)) for p in new_positions}

    for ticker in new_tickers:
        old_v = old_values.get(ticker, 0)
        new_v = new_values.get(ticker, 0)
        if old_v > 0 and abs(new_v - old_v) / old_v > 0.10:
            logger.info("Portfolio change: %s value shifted %.1f%%",
                         ticker, (new_v - old_v) / old_v * 100)
            return True

    return False


def evaluate_clusters(
    positions: list[dict],
    manual_assets: list[dict] | None = None,
    total_nav: float = 0,
    cluster_threshold_pct: float = 10.0,
) -> list[dict]:
    """Call Claude Haiku to identify correlation clusters.

    Args:
        positions: IB positions with ticker, description, sector, asset_class, base_market_value
        manual_assets: Off-platform assets with name, asset_type, value
        total_nav: Total portfolio NAV for percentage calculations
        cluster_threshold_pct: Minimum combined weight to flag a cluster

    Returns:
        List of cluster dicts: {name, positions, combined_value, combined_pct,
                                stress_scenario, severity}
    """
    import anthropic

    if total_nav <= 0:
        return []

    # Build position summary for the prompt
    pos_lines = []
    for p in positions:
        ticker = p.get("ticker", "?")
        desc = p.get("description", "")
        sector = p.get("sector", "")
        ac = p.get("asset_class", "")
        mv = abs(p.get("base_market_value", 0))
        pct = mv / total_nav * 100
        pos_lines.append(f"- {ticker} ({desc}): ${mv:,.0f} ({pct:.1f}%) [sector={sector}, class={ac}]")

    manual_lines = []
    for a in (manual_assets or []):
        name = a.get("name", "?")
        atype = a.get("asset_type", "")
        val = a.get("value", 0)
        pct = val / total_nav * 100
        manual_lines.append(f"- {name}: ${val:,.0f} ({pct:.1f}%) [type={atype}]")

    user_message = f"""Analyze this portfolio for correlation clusters. Total NAV: ${total_nav:,.0f}

ON-PLATFORM POSITIONS (IB):
{chr(10).join(pos_lines) if pos_lines else "(none)"}

OFF-PLATFORM ASSETS:
{chr(10).join(manual_lines) if manual_lines else "(none)"}

Identify all correlation clusters where the combined weight exceeds {cluster_threshold_pct}% of NAV."""

    # Tool definition for structured output
    cluster_tool = {
        "name": "report_clusters",
        "description": "Report identified correlation clusters in the portfolio",
        "input_schema": {
            "type": "object",
            "properties": {
                "clusters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Short cluster name (e.g., 'Crypto/Fintech')"},
                            "positions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of position names/tickers in this cluster",
                            },
                            "combined_value": {"type": "number", "description": "Combined USD value"},
                            "combined_pct": {"type": "number", "description": "Combined % of total NAV"},
                            "stress_scenario": {"type": "string", "description": "The scenario that would cause all positions to decline together"},
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                        "required": ["name", "positions", "combined_value", "combined_pct", "stress_scenario", "severity"],
                    },
                },
            },
            "required": ["clusters"],
        },
    }

    try:
        api_key = _get_api_key()
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[cluster_tool],
            tool_choice={"type": "tool", "name": "report_clusters"},
            messages=[{"role": "user", "content": user_message}],
        )

        # Extract tool use result
        for block in response.content:
            if block.type == "tool_use" and block.name == "report_clusters":
                clusters = block.input.get("clusters", [])
                logger.info("Cluster analysis: %d clusters identified (usage: %d in, %d out, %d cache_read)",
                            len(clusters),
                            response.usage.input_tokens,
                            response.usage.output_tokens,
                            getattr(response.usage, "cache_read_input_tokens", 0))
                return clusters

        logger.warning("Claude did not return cluster tool result")
        return []

    except Exception as e:
        logger.error("Cluster evaluation failed: %s", e)
        return []


def load_cluster_cache() -> list[dict] | None:
    """Load cached cluster analysis."""
    if not _CLUSTER_CACHE.exists():
        return None
    try:
        data = json.loads(_CLUSTER_CACHE.read_text())
        return data.get("clusters")
    except Exception:
        return None


def save_cluster_cache(clusters: list[dict], timestamp: str) -> None:
    """Save cluster analysis to cache."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CLUSTER_CACHE.write_text(json.dumps({
        "clusters": clusters,
        "timestamp": timestamp,
        "evaluated_at": datetime.now().isoformat(),
    }, indent=2))
    logger.info("Cluster cache saved: %d clusters", len(clusters))
