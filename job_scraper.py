#!/usr/bin/env python3
"""
GT Machine Job Scraper
======================
Pulls companies from the Supabase `companies` table, discovers their careers
pages, detects ATS type, scrapes jobs, filters for target titles, and upserts
matching jobs into the `jobs` table.

Reuses the same ATS detection and scraping logic as vc_monitor.py.

Required columns on `companies` table:
    careers_url text
    ats_type text
    ats_slug text
    last_scraped timestamptz

Usage:
    python job_scraper.py --all           # discover careers + scrape jobs
    python job_scraper.py --discover      # only find careers pages for companies missing one
    python job_scraper.py --scrape        # only scrape jobs (skip careers discovery)
    python job_scraper.py --company 123   # scrape a single company by id
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------
# SUPABASE CONFIG
# ----------------------------------------------------------------

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
    """GET rows from a Supabase table with query params as filters."""
    url = f"{SUPABASE_URL}/{table}"
    params["limit"] = limit
    try:
        resp = requests.get(url, headers=sb_headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json() if resp.text else []
    except Exception as e:
        log.error(f"Supabase GET {table} failed: {e}")
        return []


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


# ----------------------------------------------------------------
# TARGET TITLES (same list as vc_monitor)
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
        r"\bcfo\b", r"\bcoo\b", r"\bcro\b",
        r"\bchief of staff\b",
        r"\bchief financial officer\b",
        r"\bchief operating officer\b",
        r"\bchief revenue officer\b",
        r"\b(vp|vice president) of (finance|operations|ops|revenue operations)\b",
        r"\b(svp|senior vice president) of (finance|operations|ops)\b",
        r"\bdirector of (finance|technical accounting|accounting)\b",
        r"\bhead of (finance|operations|ops|fp&a|strategic finance|"
        r"revenue cycle|accounting operations|revenue cycle management)\b",
        r"\bhead of finance and operations\b",
        r"\bhead of ops and finance\b",
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
                  id title isRemote locationName compensationTierSummary externalLink
                }
              }
            }
        """,
    }
    try:
        resp = requests.post(url, json=query, headers=HEADERS_BROWSER, timeout=15)
        postings = resp.json().get("data", {}).get("jobBoard", {}).get("jobPostings", []) or []
        jobs = []
        for p in postings:
            location = p.get("locationName", "")
            if p.get("isRemote"):
                location = "Remote" + (f", {location}" if location else "")
            job_url = p.get("externalLink") or f"https://jobs.ashbyhq.com/{slug}/{p.get('id', '')}"
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


def scrape_generic(careers_url: str) -> list[dict]:
    resp = safe_get(careers_url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
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

    junk_url = re.compile(
        r"cookie|privacy|terms|legal|gdpr|javascript:|#cookie|#privacy"
        r"|/blog/|/press/|/news/|/about|/contact|/team|linkedin\.com/in/", re.I
    )

    for item in containers:
        if item.name == "a":
            title = item.get_text(" ", strip=True)
            href = item.get("href", "")
        else:
            h_tag = item.find(["h2", "h3", "h4", "h5", "strong"])
            title = h_tag.get_text(" ", strip=True) if h_tag else item.get_text(" ", strip=True)[:100]
            a_tag = item.find("a", href=True)
            href = a_tag["href"] if a_tag else ""

        if not title or len(title) < 5 or len(title) > 100 or title in seen:
            continue
        full_url = urljoin(careers_url, href) if href else ""
        if junk_url.search(full_url):
            continue
        seen.add(title)
        job_url = urljoin(careers_url, href) if href else careers_url

        loc_tags = item.find_all(["span", "p", "div"], class_=re.compile(r"loc|location|office", re.I))
        location = loc_tags[0].get_text(" ", strip=True) if loc_tags else ""

        jobs.append({"title": title, "location": location, "url": job_url, "salary_text": ""})
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
    if careers_url:
        return scrape_generic(careers_url)
    return []


# ----------------------------------------------------------------
# MAIN PIPELINES
# ----------------------------------------------------------------

def discover_careers():
    """Find careers pages for companies that don't have one yet."""
    rows = sb_get("companies", {
        "careers_url": "is.null",
        "select": "id,name,domain",
    })
    log.info(f"Discovering careers pages for {len(rows)} companies...")

    found = 0
    for row in rows:
        domain = row.get("domain")
        name = row.get("name")
        company_id = row.get("id")
        if not domain:
            continue

        careers_url = find_careers_url(domain)
        if not careers_url:
            log.debug(f"  No careers page: {name} ({domain})")
            continue

        # Detect ATS
        resp = safe_get(careers_url, timeout=10)
        ats_type = ats_slug = None
        if resp:
            ats_type, ats_slug, direct = detect_ats(resp.text, careers_url)
            careers_url = direct or careers_url

        sb_patch("companies", {"id": company_id}, {
            "careers_url": careers_url,
            "ats_type": ats_type,
            "ats_slug": ats_slug,
        })
        log.info(f"  Found: {name} -> {careers_url} [ATS: {ats_type or 'unknown'}]")
        found += 1
        time.sleep(0.5)

    log.info(f"Careers discovery complete. Found {found}/{len(rows)} pages.")


def scrape_jobs(company_id: Optional[int] = None):
    """Scrape jobs for all companies (or one specific company)."""
    if company_id:
        rows = sb_get("companies", {"id": f"eq.{company_id}"})
    else:
        rows = sb_get("companies", {
            "careers_url": "not.is.null",
            "select": "id,name,domain,careers_url,ats_type,ats_slug",
        })

    log.info(f"Scraping jobs for {len(rows)} companies...")
    now = datetime.utcnow().isoformat()
    total_matches = 0

    for co in rows:
        name = co.get("name", "unknown")
        careers_url = co.get("careers_url")
        if not careers_url:
            continue

        jobs = get_jobs_for_company(co)
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

            # Parse salary_min
            salary_min = None
            if salary_text:
                vals = re.findall(r"\$?([\d,]+)[kK]?", salary_text.replace(",", ""))
                nums = []
                for v in vals:
                    n = int(v)
                    if "k" in salary_text.lower() and n < 10_000:
                        n *= 1_000
                    if n >= 10_000:
                        nums.append(n)
                salary_min = min(nums) if nums else None

            # Check if job URL already exists
            existing = sb_get("jobs", {"url": f"eq.{job_url}", "limit": "1"})
            if existing:
                sb_patch("jobs", {"url": job_url}, {
                    "last_seen": now,
                    "active": True,
                })
            else:
                sb_insert("jobs", {
                    "company_name": name,
                    "rf_company_id": co.get("rf_company_id"),
                    "title": title,
                    "url": job_url,
                    "location": location,
                    "salary_text": salary_text,
                    "salary_min": salary_min,
                    "source": "scraper",
                    "first_seen": now,
                    "last_seen": now,
                    "active": True,
                })
                log.info(f"  NEW: {title} at {name} [{location}]")

            matches += 1

        # Update last_scraped on the company
        sb_patch("companies", {"id": co["id"]}, {"last_scraped": now})

        if matches:
            log.info(f"  {name}: {matches} matching job(s)")
        total_matches += matches
        time.sleep(0.5)

    log.info(f"Scraping complete. {total_matches} total matching jobs across {len(rows)} companies.")


# ----------------------------------------------------------------
# CLI
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GT Machine Job Scraper")
    parser.add_argument("--all", action="store_true", help="Discover careers + scrape jobs")
    parser.add_argument("--discover", action="store_true", help="Only discover careers pages")
    parser.add_argument("--scrape", action="store_true", help="Only scrape jobs")
    parser.add_argument("--company", type=int, help="Scrape a single company by ID")
    args = parser.parse_args()

    if not SUPABASE_KEY:
        log.error("SUPABASE_KEY not set. Export it or pass via environment variable.")
        return

    if args.company:
        scrape_jobs(company_id=args.company)
    elif args.discover:
        discover_careers()
    elif args.scrape:
        scrape_jobs()
    elif args.all:
        discover_careers()
        scrape_jobs()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
