#!/usr/bin/env python3
"""Launcher script for ib-connect MCP server."""
import sys
from pathlib import Path

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent))

from server.server import main

if __name__ == "__main__":
    main()
