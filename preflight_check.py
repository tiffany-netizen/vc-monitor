#!/usr/bin/env python3
"""Basic environment and Supabase preflight checks for the VC monitor repo."""

import argparse
import os
from typing import Optional

import requests

from env_loader import load_local_env


DEFAULT_SUPABASE_URL = "https://jhreeyesdtnmanolmjqu.supabase.co/rest/v1"
DEFAULT_TIMEOUT = 15

load_local_env()

REQUIRED_TABLES = {
    "companies": ["id", "name", "website", "careers_url", "ats_type", "ats_slug", "last_scraped"],
    "jobs": ["id", "company_name", "title", "url", "status", "last_seen"],
    "people": ["id", "name", "company_name", "status", "created_at"],
    "vc_portfolio_companies": ["id", "company", "domain", "careers_url", "ats_type", "ats_slug", "last_scraped"],
    "vc_jobs": ["id", "company_id", "title", "url", "last_seen", "active"],
}


def build_headers(supabase_key: str) -> dict:
    return {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }


def get_env_config() -> tuple[str, str]:
    supabase_url = os.environ.get("SUPABASE_URL", DEFAULT_SUPABASE_URL).rstrip("/")
    supabase_key = os.environ.get("SUPABASE_KEY", "")
    return supabase_url, supabase_key


def check_environment() -> list[str]:
    _, supabase_key = get_env_config()
    failures = []
    if not supabase_key:
        failures.append("SUPABASE_KEY is not set")
    return failures


def check_table(
    supabase_url: str,
    supabase_key: str,
    table: str,
    columns: list[str],
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    select_clause = ",".join(columns)
    url = f"{supabase_url}/{table}"
    params = {"select": select_clause, "limit": 1}

    try:
        response = requests.get(url, headers=build_headers(supabase_key), params=params, timeout=timeout)
        response.raise_for_status()
        return True, f"OK {table} ({select_clause})"
    except requests.HTTPError:
        detail = ""
        try:
            detail = response.text[:300]
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return False, f"FAIL {table}{suffix}"
    except Exception as exc:
        return False, f"FAIL {table}: {exc}"


def run_preflight(selected_tables: Optional[list[str]] = None, timeout: int = DEFAULT_TIMEOUT) -> int:
    supabase_url, supabase_key = get_env_config()
    failures = check_environment()

    if failures:
        print("Preflight failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    tables_to_check = selected_tables or list(REQUIRED_TABLES.keys())
    exit_code = 0

    print(f"Checking Supabase at {supabase_url}")
    for table in tables_to_check:
        columns = REQUIRED_TABLES.get(table)
        if not columns:
            print(f"FAIL {table}: table is not in REQUIRED_TABLES")
            exit_code = 1
            continue

        ok, message = check_table(supabase_url, supabase_key, table, columns, timeout=timeout)
        print(message)
        if not ok:
            exit_code = 1

    if exit_code == 0:
        print("Preflight passed")
    else:
        print("Preflight found issues")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run preflight checks before scraping")
    parser.add_argument(
        "--tables",
        nargs="+",
        choices=sorted(REQUIRED_TABLES.keys()),
        help="Optional subset of tables to validate",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()
    return run_preflight(selected_tables=args.tables, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())