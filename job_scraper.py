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

    patterns = [
        # C-suite
        r"\bcfo\b", r"\bcoo\b", r"\bcro\b", r"\bcao\b",
        r"\bchief of staff\b",
        r"\bchief financial officer\b",
        r"\bchief operating officer\b",
        r"\bchief revenue officer\b",
        r"\bchief accounting officer\b",
        # VP / SVP level
        r"\b(vp|vice president)\s+(of\s+)?(finance|accounting|operations|ops|"
        r"revenue operations|rev ops|business operations|fp&a|"
        r"financial planning|strategic finance|corporate development)\b",
        r"\b(svp|senior vice president)\s+(of\s+)?(finance|accounting|"
        r"operations|ops|finance and operations)\b",
        # Director level
        r"\bdirector\s+(of\s+)?(finance|accounting|technical accounting|"
        r"business operations|operations|ops|fp&a|financial planning|"
        r"strategic finance|revenue operations|rev ops|corporate development)\b",
        r"\bsenior director\s+(of\s+)?(finance|accounting|operations|ops|"
        r"business operations|fp&a|strategic finance)\b",
        # Head of
        r"\bhead of\s+(finance|accounting|accounting operations|operations|ops|"
        r"fp&a|strategic finance|revenue cycle|revenue cycle management|"
        r"corporate development|event operations|marketplace operations|"
        r"business operations|financial operations|finance and operations|"
        r"ops and finance)\b",
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
        log.debug(f"GET {url} failed: {e}")
        return None


def get_domain(url: str) -> str:
    return urlparse(url).netloc.lstrip("www.")


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

ATS_PATTERNS = [
    ("greenhouse", r"boards\.greenhouse\.io/([^/\s\"'?#]+)", 1),
    ("greenhouse", r"job-boards\.greenhouse\.io/([^/\s\"'?#]+)", 1),
    ("greenhouse", r"greenhouse\.io/embed/job_board\?for=([^&\s\"']+)", 1),
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
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            slug = m.group(group).rstrip("/").lower() if m.lastindex and group <= m.lastindex else None
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
        jobs = []
        for j in data.get("jobs", []):
            title = j.get("title", "")
            location = ", ".join(
                loc.get("name", "") for loc in j.get("offices", []) if loc.get("name")
            ) or j.get("location", {}).get("name", "")
            job_url = j.get("absolute_url", "")

            salary_text = ""
            for meta in j.get("metadata", []):
                if "salary" in meta.get("name", "").lower() or "comp" in meta.get("name", "").lower():
                    salary_text = str(meta.get("value") or "")
            if not salary_text:
                content = j.get("content", "") or ""
                m = re.search(r"\$[\d,]+\s*[-\u2013]\s*\$[\d,]+", content)
                if m:
                    salary_text = m.group(0)

            jobs.append({"title": title, "location": location, "url": job_url, "salary_text": salary_text})
        return jobs
    except Exception as e:
        log.debug(f"Greenhouse parse error ({slug}): {e}")
        return []


def scrape_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = safe_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
        jobs = []
        for j in data:
            title = j.get("text", "")
            cats = j.get("categories", {})
            location = cats.get("location", "") or (cats.get("allLocations", [""])[0] if cats.get("allLocations") else "")
            job_url = j.get("hostedUrl", "")

            salary_text = ""
            for lst in j.get("lists", []):
                if any(kw in lst.get("text", "").lower() for kw in ["salary", "compensation", "pay"]):
                    salary_text = BeautifulSoup(lst.get("content", ""), "lxml").get_text(" ")
            if not salary_text:
                plain = j.get("descriptionPlain", "") or ""
                m = re.search(r"\$[\d,]+\s*[-\u2013]\s*\$[\d,]+", plain)
                if m:
                    salary_text = m.group(0)

            jobs.append({"title": title, "location": location, "url": job_url, "salary_text": salary_text})
        return jobs
    except Exception as e:
        log.debug(f"Lever parse error ({slug}): {e}")
        return []


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
        postings = resp.json().get("data", {}).get("jobBoard", {}).get("jobPostings", []) or []
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
                loc = p.get("location", {})
                city = loc.get("city", "")
                region = loc.get("region", "")
                country = loc.get("country", "")
                location = ", ".join(part for part in [city, region, country] if part)
                comp = p.get("compensation", {})
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
        for posting in data.get("jobPostings", []):
            title = posting.get("title", "")
            loc = posting.get("locationsText", "") or posting.get("bulletFields", [""])[0]
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
            for posting in resp.json().get("jobPostings", []):
                title = posting.get("title", "")
                loc = posting.get("locationsText", "") or posting.get("bulletFields", [""])[0]
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


def scrape_generic(careers_url: str) -> list[dict]:
    """Scrape jobs from a non-ATS careers page. Falls back to Playwright for JS-rendered pages."""
    resp = safe_get(careers_url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    jobs = _extract_jobs_from_soup(soup, careers_url)

    # Fallback: if static HTML found nothing, try rendering with Playwright
    if not jobs:
        try:
            from playwright.sync_api import sync_playwright
            log.info(f"  Trying Playwright fallback for {careers_url}")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(careers_url, wait_until="networkidle", timeout=20000)
                html = page.content()
                browser.close()
            soup = BeautifulSoup(html, "lxml")
            jobs = _extract_jobs_from_soup(soup, careers_url)
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

    if ats_type and ats_type not in ("greenhouse", "lever", "ashby", "smartrec", "workday", "rippling", "dover"):
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
        return host.lstrip("www.").rstrip("/").lower()
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
        rows = sb_get(tbl, params)

    log.info(f"[{tbl}] Scraping jobs for {len(rows)} companies...")
    now = datetime.now(UTC).isoformat()
    total_matches = 0

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
                    if not sb_insert("jobs", {
                        "company_name": name,
                        "title": title,
                        "url": job_url,
                        "source": source,
                        "first_seen": now,
                        "last_seen": now,
                        "dna_fit": True,
                        "status": "new",
                    }):
                        log_write_failure("INSERT", "jobs", f"company={name} title={title} url={job_url}")

            matches += 1

        # Update last_scraped on the company
        if not sb_patch(tbl, {"id": co["id"]}, {"last_scraped": now}):
            log_write_failure("PATCH", tbl, f"company={name} id={co['id']} last_scraped={now}")

        if matches:
            log.info(f"  {name}: {matches} matching job(s)")
        total_matches += matches
        time.sleep(0.5)

    log.info(f"[{tbl}] Scraping complete. {total_matches} total matching jobs across {len(rows)} companies.")


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
    args = parser.parse_args()

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
