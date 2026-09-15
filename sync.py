import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
import requests

API_HOST = os.environ["TOAST_API_HOST"].rstrip("/")
CLIENT_ID = os.environ["TOAST_CLIENT_ID"]
CLIENT_SECRET = os.environ["TOAST_CLIENT_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]

LOCATIONS = {
    "Ritual": os.environ["TOAST_RITUAL_GUID"],
    "Dogwood": os.environ["TOAST_DOGWOOD_GUID"],
    "Madison Maison": os.environ["TOAST_MADISON_MAISON_GUID"],
}


def auth_token():
    r = requests.post(
        f"{API_HOST}/authentication/v1/authentication/login",
        json={
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["token"]["accessToken"]


def headers(token, restaurant_guid):
    return {
        "Authorization": f"Bearer {token}",
        "Toast-Restaurant-External-ID": restaurant_guid,
        "Content-Type": "application/json",
    }


def toast_dt(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"


def get_json(token, restaurant_guid, path, params=None):
    r = requests.get(
        f"{API_HOST}{path}",
        headers=headers(token, restaurant_guid),
        params=params or {},
        timeout=90,
    )
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for {r.url}: {r.text[:1000]}",
            response=r,
        )
    return r.json()


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS toast_locations (
            restaurant_guid text PRIMARY KEY,
            location_name text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS toast_orders_raw (
            restaurant_guid text NOT NULL,
            location_name text NOT NULL,
            order_guid text NOT NULL,
            modified_date timestamptz,
            business_date integer,
            opened_date timestamptz,
            closed_date timestamptz,
            raw jsonb NOT NULL,
            synced_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (restaurant_guid, order_guid)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS toast_time_entries_raw (
            restaurant_guid text NOT NULL,
            location_name text NOT NULL,
            time_entry_guid text NOT NULL,
            business_date integer,
            modified_date timestamptz,
            raw jsonb NOT NULL,
            synced_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (restaurant_guid, time_entry_guid)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS toast_menus_raw (
            restaurant_guid text PRIMARY KEY,
            location_name text NOT NULL,
            last_updated text,
            raw jsonb NOT NULL,
            synced_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS toast_sync_runs (
            id bigserial PRIMARY KEY,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            status text NOT NULL,
            detail jsonb
        )
    """)


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def sync_orders(conn, token, name, guid, start, end):
    page = 1
    count = 0
    while True:
        data = get_json(
            token,
            guid,
            "/orders/v2/ordersBulk",
            params={
                "startDate": toast_dt(start),
                "endDate": toast_dt(end),
                "page": page,
                "pageSize": 100,
            },
        )
        if not data:
            break
        for order in data:
            order_guid = order.get("guid")
            if not order_guid:
                continue
            closed = None
            checks = order.get("checks") or []
            closed_candidates = [parse_dt(c.get("closedDate")) for c in checks if c.get("closedDate")]
            closed_candidates = [x for x in closed_candidates if x]
            if closed_candidates:
                closed = max(closed_candidates)
            conn.execute(
                """
                INSERT INTO toast_orders_raw
                  (restaurant_guid, location_name, order_guid, modified_date, business_date, opened_date, closed_date, raw, synced_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,now())
                ON CONFLICT (restaurant_guid, order_guid) DO UPDATE SET
                  location_name=EXCLUDED.location_name,
                  modified_date=EXCLUDED.modified_date,
                  business_date=EXCLUDED.business_date,
                  opened_date=EXCLUDED.opened_date,
                  closed_date=EXCLUDED.closed_date,
                  raw=EXCLUDED.raw,
                  synced_at=now()
                """,
                (
                    guid, name, order_guid, parse_dt(order.get("modifiedDate")), order.get("businessDate"),
                    parse_dt(order.get("openedDate")), closed, json.dumps(order),
                ),
            )
            count += 1
        if len(data) < 100:
            break
        page += 1
    return count


def sync_labor(conn, token, name, guid, start, end):
    data = get_json(
        token,
        guid,
        "/labor/v1/timeEntries",
        params={
            "modifiedStartDate": toast_dt(start),
            "modifiedEndDate": toast_dt(end),
            "includeArchived": "true",
            "includeMissedBreaks": "true",
        },
    )
    count = 0
    for entry in data or []:
        entry_guid = entry.get("guid")
        if not entry_guid:
            continue
        conn.execute(
            """
            INSERT INTO toast_time_entries_raw
              (restaurant_guid, location_name, time_entry_guid, business_date, modified_date, raw, synced_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,now())
            ON CONFLICT (restaurant_guid, time_entry_guid) DO UPDATE SET
              location_name=EXCLUDED.location_name,
              business_date=EXCLUDED.business_date,
              modified_date=EXCLUDED.modified_date,
              raw=EXCLUDED.raw,
              synced_at=now()
            """,
            (guid, name, entry_guid, entry.get("businessDate"), parse_dt(entry.get("modifiedDate")), json.dumps(entry)),
        )
        count += 1
    return count


def sync_menu(conn, token, name, guid):
    data = get_json(token, guid, "/menus/v2/menus")
    conn.execute(
        """
        INSERT INTO toast_menus_raw (restaurant_guid, location_name, last_updated, raw, synced_at)
        VALUES (%s,%s,%s,%s::jsonb,now())
        ON CONFLICT (restaurant_guid) DO UPDATE SET
          location_name=EXCLUDED.location_name,
          last_updated=EXCLUDED.last_updated,
          raw=EXCLUDED.raw,
          synced_at=now()
        """,
        (guid, name, data.get("lastUpdated"), json.dumps(data)),
    )
    return 1


def main():
    now = datetime.now(timezone.utc)
    lookback_hours = int(os.getenv("TOAST_LOOKBACK_HOURS", "48"))
    start = now - timedelta(hours=lookback_hours)
    run_detail = {}

    with psycopg.connect(DATABASE_URL) as conn:
        ensure_schema(conn)
        run_id = conn.execute(
            "INSERT INTO toast_sync_runs (started_at,status,detail) VALUES (%s,'running','{}'::jsonb) RETURNING id",
            (now,),
        ).fetchone()[0]
        conn.commit()

        try:
            token = auth_token()
            for name, guid in LOCATIONS.items():
                conn.execute(
                    """
                    INSERT INTO toast_locations (restaurant_guid, location_name, updated_at)
                    VALUES (%s,%s,now())
                    ON CONFLICT (restaurant_guid) DO UPDATE SET location_name=EXCLUDED.location_name, updated_at=now()
                    """,
                    (guid, name),
                )
                run_detail[name] = {
                    "orders": sync_orders(conn, token, name, guid, start, now),
                    "time_entries": sync_labor(conn, token, name, guid, start, now),
                    "menus": sync_menu(conn, token, name, guid),
                }
                conn.commit()

            conn.execute(
                "UPDATE toast_sync_runs SET finished_at=now(), status='success', detail=%s::jsonb WHERE id=%s",
                (json.dumps(run_detail), run_id),
            )
            conn.commit()
            print(json.dumps({"status": "success", "detail": run_detail}, indent=2))
        except Exception as exc:
            conn.rollback()
            conn.execute(
                "UPDATE toast_sync_runs SET finished_at=now(), status='failed', detail=%s::jsonb WHERE id=%s",
                (json.dumps({"error": str(exc)}), run_id),
            )
            conn.commit()
            raise


if __name__ == "__main__":
    main()
