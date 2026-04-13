# VC Monitor Test Workflow

This repo now has a small pytest suite focused on the highest-risk failure points in the scraper pipeline.

## Setup

Install test dependencies:

```bash
pip install -r requirements-test.txt
```

Optional local credentials file (so you do not need to export env vars every run):

```bash
cat > .env << 'EOF'
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co/rest/v1
SUPABASE_KEY=YOUR_SERVICE_ROLE_KEY
EOF
```

The scripts automatically load `.env` from this folder.

Optional preflight check before scraping:

```bash
python preflight_check.py
```

If rows are still `status=pending` (or status is blank/null), approve them in code before careers/job scan.
Rows with `status=active` are also included by the scanner.

```bash
python vc_monitor.py --approve-pending
```

Typical real-db run order:

```bash
python vc_monitor.py --approve-pending --find-careers --scan-jobs
```

Run the suite:

```bash
python -m pytest -q
```

Run the dashboard-side tests:

```bash
node --test tests/dashboard_data.test.mjs
```

## What The Tests Cover

- Supabase query parameter construction in the VC monitor client
- Graceful handling of Supabase HTTP failures in Python helpers
- Title, location, and salary filtering behavior
- Insert vs update behavior for duplicate job URLs
- VC job mirroring into the main `jobs` table for dashboard visibility
- Error logging when Supabase writes fail
- Discover to scrape handoff using mocked dependencies
- CLI argument routing for `--discover`, `--scrape`, and `--og-only`
- Dashboard data fetch failures and empty-state behavior

## How To Read Failures

- A failure around `select_where` points to malformed Supabase REST filters.
- A failure around dashboard fetch handling means the UI may hang or crash on Supabase 4xx/5xx responses.
- A failure around VC mirroring means jobs may exist in `vc_jobs` but not show in the dashboard.
- A failure around duplicate URL handling means reruns may create duplicates instead of refreshing `last_seen`.
- A failure around discover to scrape means missing or stale `careers_url` values may block scraping.
- A failure around write logging usually means the scraper is no longer surfacing why Supabase writes are failing.

## Common Next Checks

If the suite passes but the app still shows no results:

1. Confirm `SUPABASE_URL` and `SUPABASE_KEY` are set for the process that is running the scraper.
2. Confirm the target tables actually exist and match the expected columns.
3. Confirm companies have a valid `careers_url` and are not filtered out before insert.
4. Check scraper logs for the new `Supabase INSERT failed` and `Supabase PATCH failed` messages.

## Preflight Check

Use the preflight script before a real run when credentials are available:

```bash
python preflight_check.py
```

To check only a subset of tables:

```bash
python preflight_check.py --tables companies jobs vc_portfolio_companies vc_jobs
```

What it verifies:

- `SUPABASE_KEY` is present
- Supabase responds to REST requests
- Required tables exist
- Required columns for each checked table are selectable

What it does not verify:

- Whether the credentials point at the correct production project
- Whether the scraper will find live jobs on external sites
- Whether scheduled GitHub secrets are configured correctly