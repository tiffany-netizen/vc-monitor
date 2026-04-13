#!/usr/bin/env python3
"""Tiny .env loader used by local scripts.

Loads key=value pairs from a file into process environment variables.
Existing environment variables are preserved by default.
"""

from __future__ import annotations

import os
from pathlib import Path


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_local_env(file_name: str = ".env", override: bool = False) -> bool:
    """Load KEY=VALUE lines from a local .env file.

    Returns True when the file exists and was parsed, otherwise False.
    """
    env_path = Path(__file__).resolve().parent / file_name
    if not env_path.exists():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_matching_quotes(value.strip())

        if not key:
            continue
        if key in os.environ and not override:
            continue

        os.environ[key] = value

    return True
