#!/usr/bin/env python3
"""
Detach people from the company they have already left.

Dry run by default. Pass --apply to write. The job-scan workflow runs it with
--apply immediately after the Make enrichment chain.

Why this exists
---------------
Enrichment ("contact enrichment", Make 4657611) records a job change by writing the
TEXT of the move: company_name, previous_company, previous_title, change_type. It
never writes people.company_id. The Roles feed joins contacts to roles on company_id,
not on company_name, so a person who left keeps being offered as the way in to the
company they left. When this was first measured, 121 of the 122 rows at
status='job_changed' were still linked to the company the person had left.

Running this straight after enrichment closes that gap: enrichment says who moved,
this moves the link.

The rule
--------
Act only where the record itself says the link is the OLD employer:

    change_type = 'company_change'  AND  job_change_confirmed = true
    AND previous_company names the company the person is still linked to
    AND company_name (where they are now) names a different company

Then:
  * the new employer resolves to exactly one real company row -> repoint (a new way in)
  * it resolves to nothing, or to a placeholder name              -> unlink (no way in)

HARD RULES - do not relax these
-------------------------------
1. Never resolve a company from company_name text alone. An earlier version keyed off
   "company_name disagrees with the linked company" and would have unlinked 182 people
   who never moved: "Midi" vs "Midi Health", "SignalFire" vs "SignalFire - Full-time".
2. Never fill a NULL company_id. A NULL means the person left and we do not track where
   they went. Filling it from text re-attaches movers to companies they left.
3. Never touch investors (contact_type='vc_partner'). A partner does not work at the
   portfolio company, so a job change there is not our signal.
4. Never link anyone to a placeholder company row ("Stealth", "Various Startups", ...).
5. Keep one step of history and no more: previous_company / previous_title are filled
   only when empty, never overwritten.

Every write is guarded on the company_id that was read, so a concurrent change - the
company dedupe pass, say - is never clobbered.
"""

import json
import logging
import os
import re
import sys
import collections
import urllib.error
import urllib.parse
import urllib.request

# Deliberately stdlib-only, and deliberately NOT importing job_scraper. This step runs
# after the scrape, so it must still work when a scraping dependency (bs4, playwright)
# is missing or broken. The two Supabase helpers below are the whole cost of that.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reconcile_links")

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://jhreeyesdtnmanolmjqu.supabase.co/rest/v1").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

LIVE_STATUSES = ("new", "active")


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def sb_get(table, params, page=1000):
    """GET rows, paginated. PostgREST caps rows per response, so always page."""
    out, offset = [], 0
    while True:
        q = dict(params, limit=page, offset=offset)
        url = f"{SUPABASE_URL}/{table}?{urllib.parse.urlencode(q, safe='.,()*')}"
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=60) as resp:
            batch = json.loads(resp.read() or b"[]")
        out.extend(batch)
        if len(batch) < page:
            return out
        offset += page


def sb_patch(table, filters, data):
    """PATCH rows matching every filter as an equality. Returns True on success."""
    q = "&".join(f"{k}=eq.{urllib.parse.quote(str(v))}" for k, v in filters.items())
    req = urllib.request.Request(f"{SUPABASE_URL}/{table}?{q}",
                                 data=json.dumps(data).encode(),
                                 headers=_headers(), method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=60):
            return True
    except urllib.error.HTTPError as e:
        log.error(f"[links] PATCH {table} failed {e.code}: {e.read().decode()[:200]}")
        return False

# Never a way in to anything: these name a state, not an employer.
PLACEHOLDER_RE = re.compile(
    r"^(stealth|stealth (ai )?startup|various startups?|self employed|freelance|"
    r"consultant|consulting|independent|retired|unemployed|confidential|n a|none|"
    r"tbd|unknown)$")


def norm(s):
    """Conservative company-name normaliser. It decides whether we write a foreign key,
    so it must not merge two genuinely different firms."""
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"[‘’“”']", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|the|plc|gmbh|ag|sa|bv|ab|oy|pte|pty)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def names_of(c):
    """Every name a company answers to: its own plus any aliases. The dedupe pass folds
    each merged duplicate's name into the survivor's aliases, so this gets more accurate
    over time and unlinks fewer people by mistake."""
    out = {norm(c.get("name"))}
    aliases = c.get("aliases")
    if isinstance(aliases, list):
        for a in aliases:
            if isinstance(a, str):
                out.add(norm(a))
    return {n for n in out if n}


def same_company(text, comp):
    """True when a free-text employer names the company row, allowing for the way the
    same firm gets written down twice: "Midi" / "Midi Health", "Furey" /
    "Furey (www.fureyfs.com)". One name being a whole-word prefix of the other is a
    variant; sharing a middle word is not."""
    t = norm(text)
    if not t:
        return False
    for n in names_of(comp):
        if t == n:
            return True
        a, b = t.split(), n.split()
        if a and b and (a[:len(b)] == b or b[:len(a)] == a):
            return True
    return False


def is_placeholder(text):
    return bool(PLACEHOLDER_RE.match(norm(text)))


def plan():
    """Read-only. Returns (repoint, unlink, contradictory, skipped, live_cids)."""
    people = sb_get("people", {
        "select": ("id,name,title,company_id,company_name,contact_type,status,"
                   "previous_company,previous_title,change_type,job_change_confirmed"),
        "status": "neq.archived",
    })
    comps = sb_get("companies", {"select": "id,name,aliases,dna_fit,excluded_at"})
    jobs = sb_get("jobs", {"select": "company_id,status"})

    live_cids = {j["company_id"] for j in jobs
                 if j.get("company_id") and j.get("status") in LIVE_STATUSES}
    cby = {c["id"]: c for c in comps}
    by_name = collections.defaultdict(list)
    for c in comps:
        for n in names_of(c):
            by_name[n].append(c)

    repoint, unlink, contradictory = [], [], []
    skipped = collections.Counter()

    for p in people:
        if p.get("contact_type") == "vc_partner":
            skipped["investor"] += 1
            continue
        cid = p.get("company_id")
        if not cid:
            skipped["no_link"] += 1
            continue
        if p.get("change_type") != "company_change":
            skipped["no_company_change"] += 1
            continue
        if p.get("job_change_confirmed") is not True:
            skipped["change_unconfirmed"] += 1
            continue
        linked = cby.get(cid)
        if not linked:
            skipped["dangling_link"] += 1
            continue
        now, was = p.get("company_name"), p.get("previous_company")
        if same_company(now, linked):
            skipped["still_there"] += 1
            continue
        if not same_company(was, linked):
            # Two contradictory claims in one record - enrichment wrote the same value
            # into current and former. Apollo settles these, not this script.
            contradictory.append((p, linked))
            continue

        matches = {c["id"]: c for c in by_name.get(norm(now), []) if c["id"] != cid}
        if len(matches) == 1 and not is_placeholder(now):
            repoint.append((p, linked, next(iter(matches.values()))))
        else:
            unlink.append((p, linked))

    return repoint, unlink, contradictory, skipped, live_cids


def main(argv):
    applying = "--apply" in argv
    repoint, unlink, contradictory, skipped, live_cids = plan()
    acts = [(p, o, n) for p, o, n in repoint] + [(p, o, None) for p, o in unlink]

    log.info(f"[links] {'APPLY' if applying else 'DRY RUN'}: "
             f"{len(repoint)} repoint, {len(unlink)} unlink, "
             f"{len(contradictory)} contradictory (left alone)")
    for k, v in skipped.most_common():
        log.info(f"[links]   skipped {k}: {v}")

    def is_way_in(c):
        return bool(c) and c.get("dna_fit") is True and not c.get("excluded_at") \
            and c["id"] in live_cids

    for p, o, n in acts:
        where = n["name"] if n else "(untracked employer)"
        flag = "  *was a live-role contact*" if is_way_in(o) else ""
        log.info(f"[links]   {p.get('name')}: off {o.get('name')} -> {where}{flag}")

    if not acts:
        log.info("[links] nothing to do.")
        return 0
    if not applying:
        log.info("[links] dry run - re-run with --apply to write.")
        return 0

    ok = 0
    for p, o, n in acts:
        patch = {"company_id": (n["id"] if n else None)}
        if not (p.get("previous_company") or "").strip():
            patch["previous_company"] = o.get("name")
        if not (p.get("previous_title") or "").strip() and p.get("title"):
            patch["previous_title"] = p.get("title")
        # Guarded on the company_id we read: if anything moved this row in the
        # meantime (the dedupe pass, a human edit), the filter matches nothing.
        if sb_patch("people", {"id": p["id"], "company_id": p["company_id"]}, patch):
            ok += 1
    log.info(f"[links] patched {ok} of {len(acts)} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
