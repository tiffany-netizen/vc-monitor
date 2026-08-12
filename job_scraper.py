#!/usr/bin/env python3
"""
GT Machine Job Scraper
======================
Discovers careers pages and scrapes jobs for companies stored in Supabase.
Works against two tables:
  - companies (machine list)
  - vc_portfolio_companies (VC list, manually imported from Crunchbase)

Usage:
    python job_scraper.py --all                          # both tables: discover + scrape
    python job_scraper.py --discover                     # discover careers pages (both tables)
    python job_scraper.py --scrape                       # scrape jobs (both tables)
    python job_scraper.py --table companies --discover   # discover for machine list only
    python job_scraper.py --table vc --discover          # discover for VC list only
    python job_scraper.py --table vc --scrape            # scrape jobs for VC list only
    python job_scraper.py --company 123                  # scrape a single company by id (companies table)

Supabase prerequisites:
    - vc_portfolio_companies must have: careers_url, ats_type, ats_slug, last_scraped columns
    - vc_jobs table for VC job inserts
    - jobs table for companies job inserts
    - RLS disabled (or service-role key) on all target tables
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from env_loader import load_local_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# SUPABASE CONFIG
# ----------------------------------------------------------------

load_local_env()

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://jhreeyesdtnmanolmjqu.supabase.co/rest/v1",
)
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY",
    "",  # set via env var or GitHub Secret
)

# ----------------------------------------------------------------
# MAKE.COM ENRICHMENT CHAINING
# ----------------------------------------------------------------
# After a scrape, kick off enrichment in Make so the pipeline runs as one flow
# (scrape -> company enrich -> contact enrich) instead of on a disconnected
# schedule. Opt-in: only fires when MAKE_API_KEY is set (the GitHub Actions
# secret), so local/manual runs are unaffected.

MAKE_API_KEY = os.environ.get("MAKE_API_KEY", "")
MAKE_API_BASE = os.environ.get("MAKE_API_BASE", "https://us1.make.com/api/v2")
# Scenarios run in order after a scrape. Both must be ACTIVE in Make to run via API.
#   4661650 = "S10: Apollo Org Enrichment"  (companies: funding / investors)
#   4657611 = "contact enrichment"          (people: title/company + job changes)
ENRICH_CHAIN = [
    (4661650, "S10 company enrichment"),
    (4657611, "contact enrichment"),
]

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def sb_headers(prefer: str = "return=minimal") -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


# ----------------------------------------------------------------
# SUPABASE HELPERS
# ----------------------------------------------------------------

def sb_get(table: str, params: dict, limit: int = 1000) -> list[dict]:
    """GET rows from a Supabase table with query params as filters. Paginates automatically."""
    url = f"{SUPABASE_URL}/{table}"
    all_rows = []
    offset = 0
    while True:
        params["limit"] = limit
        params["offset"] = offset
        try:
            resp = requests.get(url, headers=sb_headers(), params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json() if resp.text else []
        except Exception as e:
            log.error(f"Supabase GET {table} failed: {e}")
            break
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_rows


def sb_patch(table: str, filters: dict, data: dict) -> bool:
    """PATCH (update) rows matching filters."""
    url = f"{SUPABASE_URL}/{table}"
    filter_parts = [f"{k}=eq.{v}" for k, v in filters.items()]
    full_url = f"{url}?{'&'.join(filter_parts)}" if filter_parts else url
    try:
        resp = requests.patch(full_url, json=data, headers=sb_headers(), timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Supabase PATCH {table} failed: {e}")
        return False


def sb_patch_where(table: str, params: dict, data: dict) -> int:
    """PATCH rows matching raw query params. Unlike sb_patch, the param VALUES carry their
    own operator (e.g. {"last_seen": "lt.2026-..."}), so range/list filters work. Returns
    the number of rows updated, or -1 on failure."""
    url = f"{SUPABASE_URL}/{table}"
    try:
        resp = requests.patch(
            url, params=params, json=data,
            headers=sb_headers("return=representation"), timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json() if resp.text else []
        return len(rows)
    except Exception as e:
        log.error(f"Supabase PATCH-where {table} failed: {e}")
        return -1


def sb_upsert(table: str, data: dict | list[dict]) -> bool:
    """POST with upsert (merge on conflict)."""
    url = f"{SUPABASE_URL}/{table}"
    try:
        resp = requests.post(
            url, json=data,
            headers=sb_headers("resolution=merge-duplicates,return=minimal"),
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Supabase UPSERT {table} failed: {e}")
        return False


def sb_insert(table: str, data: dict) -> bool:
    url = f"{SUPABASE_URL}/{table}"
    try:
        resp = requests.post(url, json=data, headers=sb_headers(), timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.debug(f"Supabase INSERT {table} failed: {e}")
        return False


def log_write_failure(action: str, table: str, context: str):
    log.error(f"Supabase {action} failed for {table}: {context}")


# ----------------------------------------------------------------
# TARGET TITLES
# ----------------------------------------------------------------

TARGET_TITLES = [
    "CFO",
    "Chief of Staff",
    "COO",
    "CRO",
    "Director of Finance",
    "Director of Technical Accounting",
    "Head of Accounting Operations",
    "Head of Finance",
    "Head of Finance and Operations",
    "Head of FP&A",
    "Head of Operations",
    "Head of Operations and Finance",
    "Head of Ops and Finance",
    "Head of Revenue Cycle Management",
    "Head of Strategic Finance",
    "SVP of Finance",
    "SVP of Finance and Operations",
    "SVP of Operations",
    "Vice President of Revenue Operations",
    "VP of Operations",
]

MIN_SALARY = 200_000


def title_matches(title: str) -> bool:
    t = title.lower().strip()
    if len(t) > 100:
        return False
    junk = [
        "cookie", "privacy policy", "gdpr", "javascript:", "visit site",
        "read more", "learn more", "sign up", "log in", "subscribe",
    ]
    if any(p in t for p in junk):
        return False

    # Seniority and function are separated by a space ("Director of Finance"),
    # a comma ("Director, Finance"), or a slash. ATS platforms use all three, and
    # requiring whitespace silently dropped every comma-form title.
    sep = r"(?:\s*[,/]\s*|\s+)(?:of\s+)?"

    patterns = [
        # C-suite
        r"\bcfo\b", r"\bcoo\b", r"\bcro\b", r"\bcao\b",
        r"\bchief of staff\b",
        r"\bchief financial officer\b",
        r"\bchief operating officer\b",
        r"\bchief revenue officer\b",
        r"\bchief accounting officer\b",
        # VP / SVP level
        r"\b(vp|vice president)" + sep + r"(finance|accounting|operations|ops|"
        r"revenue operations|rev ops|business operations|fp&a|"
        r"financial planning|strategic finance|corporate development)\b",
        r"\b(svp|senior vice president)" + sep + r"(finance|accounting|"
        r"operations|ops|finance and operations)\b",
        # Director level. "sr." is as common as "senior" in ATS titles.
        r"\b(?:sr\.?|senior)?\s*director" + sep + r"(finance|accounting|"
        r"technical accounting|business operations|operations|ops|fp&a|"
        r"financial planning|strategic finance|revenue operations|rev ops|"
        r"corporate development)\b",
        # Function-first word order: "Finance Director", "Finance Manager" is
        # deliberately NOT included - manager level is below the target band.
        r"\b(finance|accounting|operations|fp&a|strategic finance)\s+director\b",
        # Head of
        r"\bhead of" + sep + r"(finance|accounting|accounting operations|"
        r"operations|ops|fp&a|strategic finance|revenue cycle|"
        r"revenue cycle management|corporate development|event operations|"
        r"marketplace operations|business operations|financial operations|"
        r"finance and operations|ops and finance)\b",
        # Controller
        r"\bcontroller\b",
        # Strategic finance (standalone title)
        r"^strategic finance$",
    ]
    return any(re.search(p, t) for p in patterns)


def is_us_location(location: str) -> bool:
    if not location or not location.strip():
        return True
    loc = location.lower()
    non_us = [
        "london", " uk", "united kingdom", "england", "canada", "toronto",
        "vancouver", "india", "bangalore", "bengaluru", "mumbai", "hyderabad",
        "australia", "sydney", "melbourne", "germany", "berlin", "france",
        "paris", "singapore", "hong kong", "mexico", "brazil", "netherlands",
        "amsterdam", "sweden", "stockholm", "ireland", "dublin", "israel",
        "tel aviv", "spain", "madrid", "barcelona", "poland", "warsaw",
        "japan", "tokyo", "korea", "seoul", "china", "beijing", "shanghai",
    ]
    return not any(kw in loc for kw in non_us)


def salary_qualifies(salary_text: str) -> bool:
    if not salary_text or not salary_text.strip():
        return True
    text = salary_text.replace(",", "").lower()
    matches = re.findall(r"\$?(\d+(?:\.\d+)?)([km]?)", text)
    values = []
    for num_str, suffix in matches:
        val = float(num_str)
        if suffix == "k":
            val *= 1_000
        elif suffix == "m":
            val *= 1_000_000
        if val >= 10_000:
            values.append(int(val))
    if not values:
        return True
    return min(values) >= MIN_SALARY


# ----------------------------------------------------------------
# URL / HTTP HELPERS
# ----------------------------------------------------------------

def safe_get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        # WARNING (not debug) so ATS fetch failures — 403 (IP block) / 429 (rate limit) /
        # timeout — are visible in the run log instead of silently becoming "0 jobs".
        log.warning(f"GET {url} failed: {e}")
        return None


def get_domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def url_is_live(url: str, timeout: int = 8) -> bool:
    """Return False only if the URL is definitively gone (HTTP 404/410).
    Fail-open on everything else (timeouts, bot-blocks, 5xx, redirects) so we
    never drop a real posting that simply doesn't like our request."""
    if not url:
        return False
    try:
        resp = requests.head(url, headers=HEADERS_BROWSER, timeout=timeout, allow_redirects=True)
        if resp.status_code == 405:  # HEAD not allowed — confirm with GET
            resp = requests.get(url, headers=HEADERS_BROWSER, timeout=timeout, allow_redirects=True)
        return resp.status_code not in (404, 410)
    except Exception:
        return True


# ----------------------------------------------------------------
# CAREERS PAGE DETECTION
# ----------------------------------------------------------------

CAREERS_PATHS = [
    "/careers", "/jobs", "/open-roles", "/work-with-us", "/join-us",
    "/join", "/hiring", "/openings", "/company/careers", "/about/careers",
    "/careers/", "/jobs/", "/work-here", "/careers/open-positions",
]

# Path segments that are part of Greenhouse's own URL structure, never a board
# slug. Without this guard, "boards.greenhouse.io/embed/job_board?for=acme"
# captured "embed" as the slug and every later request 404'd.
ATS_RESERVED_SLUGS = {"embed", "job_board", "jobs", "job", "boards", "board"}

ATS_PATTERNS = [
    # The embed form must be tried BEFORE the generic boards.greenhouse.io
    # pattern, otherwise the generic one matches first and captures "embed".
    ("greenhouse", r"greenhouse\.io/embed/job_board\?for=([^&\s\"']+)", 1),
    ("greenhouse", r"boards\.greenhouse\.io/([^/\s\"'?#]+)", 1),
    ("greenhouse", r"job-boards\.greenhouse\.io/([^/\s\"'?#]+)", 1),
    ("lever", r"jobs\.lever\.co/([^/\s\"'?#]+)", 1),
    ("ashby", r"jobs\.ashbyhq\.com/([^/\s\"'?#]+)", 1),
    ("workday", r"([a-zA-Z0-9-]+)\.wd\d+\.myworkdayjobs\.com", 1),
    ("bamboohr", r"([a-zA-Z0-9-]+)\.bamboohr\.com/careers", 1),
    ("rippling", r"app\.rippling\.com/jobs/([^/\s\"'?#]+)", 1),
    ("smartrec", r"careers\.smartrecruiters\.com/([^/\s\"'?#]+)", 1),
    ("jobvite", r"jobs\.jobvite\.com/([^/\s\"'?#]+)", 1),
    ("breezy", r"([a-zA-Z0-9-]+)\.breezy\.hr/p/", 1),
    ("dover", r"app\.dover\.com/apply/([^/\s\"'?#]+)", 1),
    ("wellfound", r"wellfound\.com/company/([^/\s\"'?#]+)/jobs", 1),
]


def detect_ats(html: str, base_url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    content = html + " " + base_url
    for ats_type, pattern, group in ATS_PATTERNS:
        for m in re.finditer(pattern, content, re.IGNORECASE):
            slug = m.group(group).rstrip("/").lower() if m.lastindex and group <= m.lastindex else None
            # A reserved segment means we matched Greenhouse's own URL scaffolding,
            # not a company board. Keep scanning: the real slug usually appears
            # elsewhere on the page (e.g. in the embed's ?for= parameter).
            if slug in ATS_RESERVED_SLUGS:
                continue
            if ats_type == "greenhouse":
                direct = f"https://boards.greenhouse.io/{slug}"
            elif ats_type == "lever":
                direct = f"https://jobs.lever.co/{slug}"
            elif ats_type == "ashby":
                direct = f"https://jobs.ashbyhq.com/{slug}"
            else:
                direct = None
            return ats_type, slug, direct
    return None, None, None


def find_careers_url(company_domain: str) -> Optional[str]:
    if not company_domain:
        return None
    base = f"https://{company_domain}"

    # Try common paths
    for path in CAREERS_PATHS:
        url = base + path
        resp = safe_get(url, timeout=8)
        if resp and resp.status_code == 200 and len(resp.text) > 300:
            soup = BeautifulSoup(resp.text, "lxml")
            text = soup.get_text().lower()
            job_signals = [
                "open role", "open position", "join our team",
                "job opening", "career opportunity", "we're hiring",
                "apply now", "view openings",
            ]
            if any(sig in text for sig in job_signals):
                return url
            ats_type, slug, direct = detect_ats(resp.text, url)
            if ats_type:
                return direct or url

    # Scan homepage for careers links
    resp = safe_get(base, timeout=10)
    if resp:
        soup = BeautifulSoup(resp.text, "lxml")
        careers_kw = ["career", "job", "hiring", "join us", "work with us", "open role"]
        for link in soup.find_all("a", href=True):
            href = link["href"].lower()
            link_text = link.get_text().lower()
            if any(kw in href or kw in link_text for kw in careers_kw):
                full_url = urljoin(base, link["href"])
                if any(s in full_url for s in ["linkedin", "twitter", "facebook", "instagram"]):
                    continue
                return full_url
    return None


# ----------------------------------------------------------------
# ATS SCRAPERS
# ----------------------------------------------------------------

def scrape_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = safe_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
    except Exception as e:
        log.warning(f"Greenhouse board unreadable ({slug}): {e}")
        return []

    jobs = []
    bad = 0
    # Per-posting guard: Greenhouse serves optional fields as explicit nulls
    # (e.g. "metadata": null), so a single posting used to raise inside the loop
    # and the old board-level except discarded EVERY job on that board.
    for j in data.get("jobs") or []:
        try:
            title = j.get("title", "")
            location = ", ".join(
                loc.get("name", "") for loc in (j.get("offices") or []) if loc.get("name")
            ) or (j.get("location") or {}).get("name", "")
            job_url = j.get("absolute_url", "")

            salary_text = ""
            for meta in j.get("metadata") or []:
                name = (meta.get("name") or "").lower()
                if "salary" in name or "comp" in name:
                    salary_text = str(meta.get("value") or "")
            if not salary_text:
                content = j.get("content", "") or ""
                m = re.search(r"\$[\d,]+\s*[-\u2013]\s*\$[\d,]+", content)
                if m:
                    salary_text = m.group(0)

            jobs.append({"title": title, "location": location, "url": job_url, "salary_text": salary_text})
        except Exception as e:
            bad += 1
            log.debug(f"Greenhouse posting skipped ({slug}): {e}")
    if bad:
        log.warning(f"Greenhouse {slug}: skipped {bad} malformed posting(s), kept {len(jobs)}")
    return jobs


def scrape_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = safe_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
    except Exception as e:
        log.warning(f"Lever board unreadable ({slug}): {e}")
        return []

    jobs = []
    bad = 0
    for j in data or []:
        try:
            title = j.get("text", "")
            cats = j.get("categories") or {}
            all_locs = cats.get("allLocations") or []
            location = cats.get("location") or (all_locs[0] if all_locs else "")
            job_url = j.get("hostedUrl", "")

            salary_text = ""
            for lst in j.get("lists") or []:
                if any(kw in (lst.get("text") or "").lower() for kw in ["salary", "compensation", "pay"]):
                    salary_text = BeautifulSoup(lst.get("content") or "", "lxml").get_text(" ")
            if not salary_text:
                plain = j.get("descriptionPlain", "") or ""
                m = re.search(r"\$[\d,]+\s*[-\u2013]\s*\$[\d,]+", plain)
                if m:
                    salary_text = m.group(0)

            jobs.append({"title": title, "location": location, "url": job_url, "salary_text": salary_text})
        except Exception as e:
            bad += 1
            log.debug(f"Lever posting skipped ({slug}): {e}")
    if bad:
        log.warning(f"Lever {slug}: skipped {bad} malformed posting(s), kept {len(jobs)}")
    return jobs


def scrape_ashby(slug: str) -> list[dict]:
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    query = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": slug},
        "query": """
            query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
              jobBoard: jobBoardWithTeams(
                organizationHostedJobsPageName: $organizationHostedJobsPageName
              ) {
                jobPostings {
                  id title locationName compensationTierSummary
                }
              }
            }
        """,
    }
    try:
        resp = requests.post(url, json=query, timeout=15)
        body = resp.json() or {}
        board = (body.get("data") or {}).get("jobBoard") or {}
        postings = board.get("jobPostings") or []
        jobs = []
        for p in postings:
            location = p.get("locationName", "")
            job_url = f"https://jobs.ashbyhq.com/{slug}/{p.get('id', '')}"
            jobs.append({
                "title": p.get("title", ""),
                "location": location,
                "url": job_url,
                "salary_text": p.get("compensationTierSummary") or "",
            })
        return jobs
    except Exception as e:
        log.debug(f"Ashby parse error ({slug}): {e}")
        return []


def scrape_smartrecruiters(slug: str) -> list[dict]:
    """SmartRecruiters public API."""
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    jobs = []
    offset = 0
    while True:
        resp = safe_get(f"{url}?offset={offset}&limit=100")
        if not resp:
            break
        try:
            data = resp.json()
            postings = data.get("content", [])
            if not postings:
                break
            for p in postings:
                try:
                    loc = p.get("location") or {}
                    city = loc.get("city", "")
                    region = loc.get("region", "")
                    country = loc.get("country", "")
                    location = ", ".join(part for part in [city, region, country] if part)
                    comp = p.get("compensation") or {}
                    salary_text = ""
                    if comp:
                        sal_min = comp.get("min", "")
                        sal_max = comp.get("max", "")
                        currency = comp.get("currency", "")
                        if sal_min or sal_max:
                            salary_text = f"{currency} {sal_min}-{sal_max}".strip()
                    jobs.append({
                        "title": p.get("name", ""),
                        "location": location,
                        "url": p.get("ref", "") or f"https://careers.smartrecruiters.com/{slug}/{p.get('id', '')}",
                        "salary_text": salary_text,
                    })
                except Exception as e:
                    log.debug(f"SmartRecruiters posting skipped ({slug}): {e}")
            if len(postings) < 100:
                break
            offset += 100
        except Exception as e:
            log.debug(f"SmartRecruiters parse error ({slug}): {e}")
            break
    return jobs


def scrape_workday(slug: str, careers_url: str = "") -> list[dict]:
    """Workday JSON API scraper. Tries to extract the site name from the careers_url."""
    # Workday URLs look like: company.wd5.myworkdayjobs.com/en-US/External
    # We need both the company slug and the site path
    site = "External"  # most common default
    wd_num = "1"
    if careers_url:
        m = re.search(r"([a-zA-Z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com(?:/[a-z-]+)?/([^/?\s]+)", careers_url, re.I)
        if m:
            slug = m.group(1)
            wd_num = m.group(2)
            site = m.group(3)
        else:
            m2 = re.search(r"([a-zA-Z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com", careers_url, re.I)
            if m2:
                slug = m2.group(1)
                wd_num = m2.group(2)

    api_url = f"https://{slug}.wd{wd_num}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs"
    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    jobs = []
    try:
        resp = requests.post(api_url, json=payload, headers=HEADERS_BROWSER, timeout=15)
        if resp.status_code != 200:
            log.debug(f"Workday API returned {resp.status_code} for {slug}")
            return []
        data = resp.json()
        total = data.get("total", 0)
        for posting in data.get("jobPostings") or []:
            title = posting.get("title", "")
            bullets = posting.get("bulletFields") or [""]
            loc = posting.get("locationsText", "") or bullets[0]
            ext_path = posting.get("externalPath", "")
            job_url = f"https://{slug}.wd{wd_num}.myworkdayjobs.com/en-US/{site}{ext_path}" if ext_path else ""
            jobs.append({"title": title, "location": loc, "url": job_url, "salary_text": ""})

        # Paginate if there are more
        offset = 20
        while offset < total:
            payload["offset"] = offset
            resp = requests.post(api_url, json=payload, headers=HEADERS_BROWSER, timeout=15)
            if resp.status_code != 200:
                break
            for posting in resp.json().get("jobPostings") or []:
                title = posting.get("title", "")
                bullets = posting.get("bulletFields") or [""]
                loc = posting.get("locationsText", "") or bullets[0]
                ext_path = posting.get("externalPath", "")
                job_url = f"https://{slug}.wd{wd_num}.myworkdayjobs.com/en-US/{site}{ext_path}" if ext_path else ""
                jobs.append({"title": title, "location": loc, "url": job_url, "salary_text": ""})
            offset += 20
    except Exception as e:
        log.debug(f"Workday parse error ({slug}): {e}")
    return jobs


def scrape_rippling(slug: str) -> list[dict]:
    """Rippling public job board API."""
    url = f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
    resp = safe_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
        jobs = []
        for j in data if isinstance(data, list) else data.get("jobs", []):
            location = j.get("location", "") or j.get("workLocation", "")
            jobs.append({
                "title": j.get("title", "") or j.get("name", ""),
                "location": location,
                "url": j.get("url", "") or f"https://app.rippling.com/jobs/{slug}/{j.get('id', '')}",
                "salary_text": j.get("salary", "") or "",
            })
        return jobs
    except Exception as e:
        log.debug(f"Rippling parse error ({slug}): {e}")
        return []


def scrape_dover(slug: str) -> list[dict]:
    """Dover public job board API."""
    url = f"https://app.dover.com/api/careers-page/{slug}/jobs"
    resp = safe_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
        jobs_list = data if isinstance(data, list) else data.get("jobs", [])
        jobs = []
        for j in jobs_list:
            jobs.append({
                "title": j.get("title", "") or j.get("name", ""),
                "location": j.get("location", ""),
                "url": j.get("url", "") or f"https://app.dover.com/apply/{slug}/{j.get('id', '')}",
                "salary_text": j.get("salary_range", "") or "",
            })
        return jobs
    except Exception as e:
        log.debug(f"Dover parse error ({slug}): {e}")
        return []


def scrape_bamboohr(slug: str) -> list[dict]:
    """BambooHR public careers JSON API.

    An empty board is a real, distinguishable state here (meta.totalCount == 0),
    unlike the generic scraper where zero jobs could also mean an unparseable page.
    """
    url = f"https://{slug}.bamboohr.com/careers/list"
    resp = safe_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
        jobs = []
        for j in data.get("result") or []:
            loc = j.get("location") or {}
            parts = [loc.get("city") or "", loc.get("state") or ""]
            location = ", ".join(p for p in parts if p)
            if j.get("isRemote") and not location:
                location = "Remote"
            jobs.append({
                "title": j.get("jobOpeningName", ""),
                "location": location,
                "url": f"https://{slug}.bamboohr.com/careers/{j.get('id', '')}",
                "salary_text": "",
            })
        return jobs
    except Exception as e:
        log.debug(f"BambooHR parse error ({slug}): {e}")
        return []


def _clean_title(el) -> str:
    """First non-empty text line of an element, capped at 100 chars (Change 3).
    Dropping later lines strips appended location text, e.g.
    'Head of Business Operations' + newline + 'San Francisco, United States'."""
    for line in el.get_text("\n", strip=True).split("\n"):
        line = line.strip()
        if line:
            return line[:100]
    return ""


def _extract_jobs_from_soup(soup: BeautifulSoup, careers_url: str) -> list[dict]:
    """Shared logic for extracting jobs from a parsed HTML page."""
    jobs = []
    seen = set()

    selectors = [
        "[class*='job-item']", "[class*='job-listing']", "[class*='job-posting']",
        "[class*='opening']", "[class*='position']", "[class*='role']",
        "[class*='career-item']", "li[class*='job']", "tr[class*='job']",
        "[data-job]", "[data-position]",
    ]
    containers = []
    for sel in selectors:
        containers.extend(soup.select(sel))

    if not containers:
        containers = [
            a for a in soup.find_all("a", href=True)
            if not re.search(
                r"cookie|privacy|terms|gdpr|legal|policy|javascript:|#",
                (a.get("href", "") + a.get_text()).lower(),
            )
        ]

    # Change 2: also skip team/people profiles, leadership/staff pages, and
    # marketing pages (surveys) that get mistaken for job postings.
    junk_url = re.compile(
        r"cookie|privacy|terms|legal|gdpr|javascript:|#cookie|#privacy"
        r"|/blog/|/press/|/news/|/about|/contact|/team|/people/|/profile"
        r"|/leadership|/staff/|survey|linkedin\.com/in/", re.I
    )

    careers_norm = careers_url.rstrip("/")

    for item in containers:
        if item.name == "a":
            title = _clean_title(item)
            href = item.get("href", "")
        else:
            h_tag = item.find(["h2", "h3", "h4", "h5", "strong"])
            title = _clean_title(h_tag if h_tag else item)
            a_tag = item.find("a", href=True)
            href = a_tag["href"] if a_tag else ""

        if not title or len(title) < 5 or len(title) > 100 or title in seen:
            continue

        # Change 1: a real job needs its own per-job link — not blank, and not
        # just the careers page itself (those are "ghost" rows like m3ter/Botrista).
        if not href:
            continue
        full_url = urljoin(careers_url, href)
        if junk_url.search(full_url):
            continue
        if full_url.rstrip("/") == careers_norm:
            continue

        seen.add(title)
        job_url = full_url

        loc_tags = item.find_all(["span", "p", "div"], class_=re.compile(r"loc|location|office", re.I))
        location = loc_tags[0].get_text(" ", strip=True) if loc_tags else ""

        jobs.append({"title": title, "location": location, "url": job_url, "salary_text": ""})
    return jobs


def _render_careers_page(careers_url: str) -> list[str]:
    """Render a JS-heavy careers page headless. Returns HTML of the page and
    every iframe (embedded boards live in iframes, not the main document)."""
    from playwright.sync_api import sync_playwright
    htmls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # skip heavy assets so renders stay fast
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "media", "font")
                   else route.continue_())
        # networkidle flakes on pages with analytics beacons; a fixed settle
        # wait after DOM load is what reliably surfaces client-rendered boards
        page.goto(careers_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        for frame in page.frames:
            try:
                htmls.append(frame.content())
            except Exception:
                continue
        browser.close()
    return htmls


def scrape_generic(careers_url: str) -> list[dict]:
    """Scrape jobs from a non-ATS careers page. Falls back to Playwright for JS-rendered pages."""
    jobs = []
    resp = safe_get(careers_url)
    if resp:
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = _extract_jobs_from_soup(soup, careers_url)

    # Fallback: static fetch was blocked or found nothing -> render with Playwright
    if not jobs:
        try:
            log.info(f"  Trying Playwright fallback for {careers_url}")
            seen_urls = set()
            for html in _render_careers_page(careers_url):
                soup = BeautifulSoup(html, "lxml")
                for job in _extract_jobs_from_soup(soup, careers_url):
                    if job["url"] not in seen_urls:
                        seen_urls.add(job["url"])
                        jobs.append(job)
        except ImportError:
            log.debug("Playwright not installed, skipping JS fallback")
        except Exception as e:
            log.debug(f"Playwright fallback failed for {careers_url}: {e}")

    return jobs


def get_jobs_for_company(co: dict) -> list[dict]:
    ats_type = co.get("ats_type")
    slug = co.get("ats_slug")
    careers_url = co.get("careers_url", "")

    if ats_type == "greenhouse" and slug:
        return scrape_greenhouse(slug)
    if ats_type == "lever" and slug:
        return scrape_lever(slug)
    if ats_type == "ashby" and slug:
        return scrape_ashby(slug)
    if ats_type == "smartrec" and slug:
        return scrape_smartrecruiters(slug)
    if ats_type == "workday" and slug:
        return scrape_workday(slug, careers_url)
    if ats_type == "rippling" and slug:
        return scrape_rippling(slug)
    if ats_type == "dover" and slug:
        return scrape_dover(slug)
    if ats_type == "bamboohr" and slug:
        return scrape_bamboohr(slug)

    if ats_type and ats_type not in ("greenhouse", "lever", "ashby", "smartrec", "workday", "rippling", "dover", "bamboohr"):
        log.warning(f"  No dedicated scraper for ATS '{ats_type}' (slug: {slug}), falling back to generic")

    if careers_url:
        return scrape_generic(careers_url)
    return []


# ----------------------------------------------------------------
# TABLE CONFIG
# ----------------------------------------------------------------

TABLE_CONFIG = {
    "companies": {
        "table": "companies",
        "name_col": "name",
        "domain_col": "website",
        "jobs_table": "jobs",
        "source": "scraper",
    },
    "vc": {
        "table": "vc_portfolio_companies",
        "name_col": "company",
        "domain_col": "domain",
        "jobs_table": "vc_jobs",
        "source": "vc_scraper",
    },
}


# ----------------------------------------------------------------
# MAIN PIPELINES
# ----------------------------------------------------------------

_COMPANY_INDEX: Optional[dict] = None


def _norm_company_name(name: str) -> str:
    """Normalise a company name for equality comparison. Conservative: this
    decides whether we write a foreign key, so it must not merge two firms."""
    if not name:
        return ""
    n = re.sub(r"[^a-z0-9]+", " ", name.lower().strip())
    n = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|the|plc|gmbh|ag|bv|pte|pty)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def resolve_company_id(name: str, domain: str = "") -> Optional[int]:
    """Find the `companies` row for a VC-portfolio company, by domain then name.

    VC-portfolio roles are mirrored into the main `jobs` table, but that insert
    used to omit company_id. The Roles UI joins jobs -> companies -> dna_fit, so
    an unlinked job is invisible even when its company is approved. Returns None
    unless exactly one company matches, because a wrong link is worse than none.
    """
    global _COMPANY_INDEX
    if _COMPANY_INDEX is None:
        by_domain: dict = {}
        by_name: dict = {}
        for c in sb_get("companies", {"select": "id,name,website"}):
            cid = c.get("id")
            d = extract_domain(c.get("website"))
            if d:
                by_domain.setdefault(d, set()).add(cid)
            n = _norm_company_name(c.get("name"))
            if n:
                by_name.setdefault(n, set()).add(cid)
        _COMPANY_INDEX = {"domain": by_domain, "name": by_name}
        log.info(f"[link] company index built: {len(by_domain)} domains, {len(by_name)} names")

    d = extract_domain(domain)
    if d:
        hit = _COMPANY_INDEX["domain"].get(d)
        if hit and len(hit) == 1:
            return next(iter(hit))
    n = _norm_company_name(name)
    if n:
        hit = _COMPANY_INDEX["name"].get(n)
        if hit and len(hit) == 1:
            return next(iter(hit))
    return None


def extract_domain(website: str) -> Optional[str]:
    """Pull bare domain from a website URL."""
    if not website:
        return None
    website = website.strip()
    if not website.startswith("http"):
        website = f"https://{website}"
    try:
        parsed = urlparse(website)
        host = parsed.netloc or parsed.path.split("/")[0]
        # str.lstrip("www.") strips characters, not the prefix — it turned
        # "willreed.com" into "illreed.com" and broke discovery for every
        # domain starting with "w".
        return host.lower().rstrip("/").removeprefix("www.")
    except Exception:
        return None


def discover_careers(table_key: str, og_only: bool = False):
    """Find careers pages for companies that don't have one yet."""
    cfg = TABLE_CONFIG[table_key]
    tbl = cfg["table"]
    name_col = cfg["name_col"]
    domain_col = cfg["domain_col"]

    params = {
        "careers_url": "is.null",
        "select": f"id,{name_col},{domain_col}",
    }
    if og_only and tbl == "companies":
        params["has_og_members"] = "eq.true"
    # Only touch companies we're actively monitoring. dna_fit=true == approved or
    # provisional-pass; rejected/archived companies are dna_fit=false, so we don't waste
    # discovery on them (and never learn a careers_url for a company we've excluded).
    if tbl == "companies":
        params["dna_fit"] = "eq.true"

    rows = sb_get(tbl, params)
    log.info(f"[{tbl}] Discovering careers pages for {len(rows)} companies{'  (OG only)' if og_only else ''}...")

    found = 0
    for row in rows:
        domain = extract_domain(row.get(domain_col))
        name = row.get(name_col)
        company_id = row.get("id")
        if not domain:
            continue

        careers_url = find_careers_url(domain)
        if not careers_url:
            log.debug(f"  No careers page: {name} ({domain})")
            if not sb_patch(tbl, {"id": company_id}, {"careers_url": "none"}):
                log_write_failure("PATCH", tbl, f"company={name} id={company_id} careers_url=none")
            continue

        # Detect ATS
        resp = safe_get(careers_url, timeout=10)
        ats_type = ats_slug = None
        if resp:
            ats_type, ats_slug, direct = detect_ats(resp.text, careers_url)
            careers_url = direct or careers_url

        if not sb_patch(tbl, {"id": company_id}, {
            "careers_url": careers_url,
            "ats_type": ats_type,
            "ats_slug": ats_slug,
        }):
            log_write_failure(
                "PATCH",
                tbl,
                f"company={name} id={company_id} careers_url={careers_url} ats_type={ats_type or 'unknown'}",
            )
        log.info(f"  Found: {name} -> {careers_url} [ATS: {ats_type or 'unknown'}]")
        found += 1
        time.sleep(0.5)

    log.info(f"[{tbl}] Careers discovery complete. Found {found}/{len(rows)} pages.")


def scrape_jobs(table_key: str, company_id: Optional[int] = None, og_only: bool = False):
    """Scrape jobs for all companies (or one specific company)."""
    cfg = TABLE_CONFIG[table_key]
    tbl = cfg["table"]
    name_col = cfg["name_col"]
    domain_col = cfg["domain_col"]
    jobs_table = cfg["jobs_table"]
    source = cfg["source"]

    if company_id:
        rows = sb_get(tbl, {"id": f"eq.{company_id}"})
    else:
        params = {
            "careers_url": "neq.none",
            "select": f"id,{name_col},{domain_col},careers_url,ats_type,ats_slug"
                + (",vc_names" if table_key == "vc" else ""),
        }
        if og_only and tbl == "companies":
            params["has_og_members"] = "eq.true"
        # Only scrape companies we're actively monitoring (dna_fit=true). Rejecting or
        # archiving a company in the Review UI sets dna_fit=false, which is now the single
        # off-switch that also stops the scraper from pulling its listings — this is what
        # keeps spam sources (deals/aggregator sites like Slickdeals) out once flagged.
        if tbl == "companies":
            params["dna_fit"] = "eq.true"
        rows = sb_get(tbl, params)

    log.info(f"[{tbl}] Scraping jobs for {len(rows)} companies...")
    now = datetime.now(UTC).isoformat()
    total_matches = 0
    total_closed = 0

    for co in rows:
        name = co.get(name_col, "unknown")
        careers_url = co.get("careers_url")
        if not careers_url:
            continue

        jobs = get_jobs_for_company(co)
        if not jobs:
            log.warning(f"  {name}: 0 jobs returned (ATS: {co.get('ats_type') or 'generic'}, URL: {careers_url})")
        else:
            log.info(f"  {name}: {len(jobs)} total jobs found (ATS: {co.get('ats_type') or 'generic'})")
        matches = 0

        for job in jobs:
            title = job.get("title", "").strip()
            location = job.get("location", "").strip()
            salary_text = job.get("salary_text", "").strip()
            job_url = job.get("url", "").strip()

            if not title_matches(title):
                continue
            if not is_us_location(location):
                continue
            if not salary_qualifies(salary_text):
                continue

            # Check if job URL already exists
            existing = sb_get(jobs_table, {"url": f"eq.{job_url}", "limit": "1"})
            if existing:
                update_payload = {
                    "last_seen": now,
                }
                if table_key == "companies":
                    update_payload["dna_fit"] = True

                if not sb_patch(jobs_table, {"url": job_url}, update_payload):
                    log_write_failure("PATCH", jobs_table, f"company={name} url={job_url}")
            else:
                # Change 4: don't insert a brand-new job whose link is already dead.
                if not url_is_live(job_url):
                    log.info(f"  SKIP dead link: {title} at {name} ({job_url})")
                    continue
                if table_key == "vc":
                    if not sb_insert(jobs_table, {
                        "company_id": co.get("id"),
                        "title": title,
                        "url": job_url,
                        "location": location,
                        "salary_text": salary_text,
                        "vc_names": co.get("vc_names"),
                        "source": source,
                        "first_seen": now,
                        "last_seen": now,
                        "active": True,
                    }):
                        log_write_failure("INSERT", jobs_table, f"company={name} title={title} url={job_url}")
                else:
                    if not sb_insert(jobs_table, {
                        "company_id": co.get("id"),
                        "company_name": name,
                        "title": title,
                        "url": job_url,
                        "source": source,
                        "first_seen": now,
                        "last_seen": now,
                        "dna_fit": True,
                        "status": "new",
                    }):
                        log_write_failure("INSERT", jobs_table, f"company={name} title={title} url={job_url}")
                log.info(f"  NEW: {title} at {name}")

            if table_key == "vc":
                existing_main = sb_get("jobs", {"url": f"eq.{job_url}", "limit": "1"})
                if existing_main:
                    if not sb_patch("jobs", {"url": job_url}, {"last_seen": now, "dna_fit": True}):
                        log_write_failure("PATCH", "jobs", f"company={name} url={job_url}")
                else:
                    # Link the mirrored row to its `companies` record. Without
                    # this the Roles UI cannot reach companies.dna_fit and the
                    # job never appears, even at an approved company.
                    linked_id = resolve_company_id(name, co.get("domain") or "")
                    mirror = {
                        "company_name": name,
                        "title": title,
                        "url": job_url,
                        "source": source,
                        "first_seen": now,
                        "last_seen": now,
                        "dna_fit": True,
                        "status": "new",
                    }
                    if linked_id is not None:
                        mirror["company_id"] = linked_id
                    else:
                        log.info(f"  UNLINKED: no unique companies row for {name}")
                    if not sb_insert("jobs", mirror):
                        log_write_failure("INSERT", "jobs", f"company={name} title={title} url={job_url}")

            matches += 1

        # Update last_scraped on the company
        if not sb_patch(tbl, {"id": co["id"]}, {"last_scraped": now}):
            log_write_failure("PATCH", tbl, f"company={name} id={co['id']} last_scraped={now}")

        # Stage 4 — stale-close: every job we re-saw this run had its last_seen bumped to
        # `now`, so any still-open job for this company with an older last_seen has dropped
        # off the board. Mark it closed. Guarded on a non-empty scrape (`jobs`) so a
        # transient fetch failure (0 jobs returned) never wrongly closes live roles.
        if jobs:
            if table_key == "vc":
                n_closed = sb_patch_where(
                    jobs_table,
                    {"company_id": f"eq.{co['id']}", "last_seen": f"lt.{now}", "active": "eq.true"},
                    {"active": False},
                )
            else:
                n_closed = sb_patch_where(
                    jobs_table,
                    {"company_id": f"eq.{co['id']}", "last_seen": f"lt.{now}", "status": "in.(new,active)"},
                    {"status": "closed"},
                )
            if n_closed < 0:
                log_write_failure("PATCH", jobs_table, f"stale-close company={name} id={co['id']}")
            elif n_closed:
                total_closed += n_closed
                log.info(f"  {name}: closed {n_closed} stale job(s) no longer on the board")

        if matches:
            log.info(f"  {name}: {matches} matching job(s)")
        total_matches += matches
        time.sleep(0.5)

    log.info(f"[{tbl}] Scraping complete. {total_matches} total matching jobs across {len(rows)} companies. {total_closed} stale job(s) closed.")


# ----------------------------------------------------------------
# ENRICHMENT CHAIN
# ----------------------------------------------------------------

def trigger_enrichment_chain() -> None:
    """Run company- then contact-enrichment in Make, in sequence, after a scrape.

    Best-effort and non-fatal: a chaining failure must never fail the scrape.
    No-op unless MAKE_API_KEY is set. Each scenario must be ACTIVE in Make; an
    inactive/failed scenario is logged and skipped so the next one still runs.
    """
    if not MAKE_API_KEY:
        log.info("[chain] MAKE_API_KEY not set — skipping enrichment auto-chain.")
        return
    headers = {"Authorization": f"Token {MAKE_API_KEY}", "Content-Type": "application/json"}
    for sid, name in ENRICH_CHAIN:
        try:
            log.info(f"[chain] running {name} (scenario {sid})…")
            # Long timeout so the call blocks until the run finishes and the next
            # scenario starts after it (Apollo rate-friendly). If a run outlasts the
            # timeout the scenario still completes server-side; only the ordering is lost.
            resp = requests.post(
                f"{MAKE_API_BASE}/scenarios/{sid}/run",
                headers=headers,
                json={},
                timeout=1500,
            )
            if resp.status_code >= 400:
                log.warning(
                    f"[chain] {name} did not run [{resp.status_code}]: "
                    f"{resp.text[:200]} (is the scenario active?)"
                )
                continue
            log.info(f"[chain] {name} done [{resp.status_code}].")
        except requests.RequestException as e:
            log.warning(f"[chain] {name} trigger failed ({e}); continuing.")


# ----------------------------------------------------------------
# CLI
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GT Machine Job Scraper")
    parser.add_argument("--all", action="store_true", help="Discover careers + scrape jobs")
    parser.add_argument("--discover", action="store_true", help="Only discover careers pages")
    parser.add_argument("--scrape", action="store_true", help="Only scrape jobs")
    parser.add_argument("--table", choices=["companies", "vc", "both"], default="both",
                        help="Which table to run against (default: both)")
    parser.add_argument("--og-only", action="store_true",
                        help="Only process companies with OG members (companies table only)")
    parser.add_argument("--company", type=int, help="Scrape a single company by ID (companies table)")
    parser.add_argument("--chain-enrich", action="store_true",
                        help="Only fire the Make enrichment chain (company then contact); no scraping")
    args = parser.parse_args()

    # Enrichment chain is a standalone step (run once after both tables are scraped
    # by the workflow), so it lives outside the scrape branches and needs no SUPABASE_KEY.
    if args.chain_enrich:
        trigger_enrichment_chain()
        return

    if not SUPABASE_KEY:
        log.error("SUPABASE_KEY not set. Export it or pass via environment variable.")
        return

    tables = ["companies", "vc"] if args.table == "both" else [args.table]

    if args.company:
        scrape_jobs("companies", company_id=args.company)
    elif args.discover:
        for t in tables:
            discover_careers(t, og_only=args.og_only)
    elif args.scrape:
        for t in tables:
            scrape_jobs(t, og_only=args.og_only)
    elif args.all:
        for t in tables:
            discover_careers(t, og_only=args.og_only)
            scrape_jobs(t, og_only=args.og_only)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
