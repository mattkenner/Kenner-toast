# Kenner Group Toast Sync

Read-only Toast Standard API sync for Ritual, Dogwood, and Madison Maison.

## Data captured
- Orders (raw JSON, incrementally updated by modified timestamp)
- Labor time entries (raw JSON)
- Menus V2 (raw JSON snapshot)
- Sync run audit log

## Required Render environment variables
- `TOAST_API_HOST` — production API hostname shown by Toast for your Standard API access
- `TOAST_CLIENT_ID`
- `TOAST_CLIENT_SECRET`
- `TOAST_RITUAL_GUID`
- `TOAST_DOGWOOD_GUID`
- `TOAST_MADISON_MAISON_GUID`
- `DATABASE_URL`
- `TOAST_LOOKBACK_HOURS` — defaults to 48

Never commit real secrets to GitHub.

## Render cron configuration
- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python sync.py`
- Suggested schedule for initial production use: hourly (`0 * * * *`)
- Region: Virginia, matching the database

## First run
The sync automatically creates its tables. Start with a 48-hour lookback, verify counts, then backfill historical periods separately if desired.
