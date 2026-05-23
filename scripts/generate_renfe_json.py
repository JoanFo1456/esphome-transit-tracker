"""
Generate data/renfe-departures.json from Renfe's GTFS feed.

Outputs the next HOURS_AHEAD hours of departures at STOP_ID in the same
JSON format the transit_tracker component expects from its WebSocket server,
so the HTTP polling mode can consume it directly.

Run manually:  python scripts/generate_renfe_json.py
Run in CI:     see .github/workflows/renfe-departures.yml
"""

import json
import os
import sys
import zipfile
import io
from datetime import datetime, timedelta

import requests
import pandas as pd
import pytz

GTFS_URL = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"

STOP_ID = "22100"   # Ourense — run test.py with your city name to find yours
HOURS_AHEAD = 24    # How many hours ahead to include
LIMIT = 12          # Max trips in the JSON (ESP32 will show its own 'limit')

MADRID_TZ = pytz.timezone("Europe/Madrid")

ROUTE_COLORS = {
    "AVE":       "E3000F",
    "ALVIA":     "0064A8",
    "AVLO":      "6B2D8B",
    "AVANT":     "FF6B00",
    "Intercity": "888888",
    "MD":        "999999",
    "REGIONAL":  "777777",
    "REG.EXP.":  "666666",
    "TRENCELTA": "3366AA",
}


def parse_gtfs_time(t: str) -> timedelta:
    h, m, s = map(int, t.split(":"))
    return timedelta(hours=h, minutes=m, seconds=s)


def active_services(calendar: pd.DataFrame, calendar_dates: pd.DataFrame, date_str: str) -> set:
    date_int = int(date_str)
    dt = datetime.strptime(date_str, "%Y%m%d")
    dow = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][dt.weekday()]

    if not calendar.empty and dow in calendar.columns:
        regular = set(calendar.loc[
            (calendar["start_date"] <= date_int) &
            (calendar["end_date"] >= date_int) &
            (calendar[dow] == 1),
            "service_id"
        ])
    else:
        regular = set()

    additions = set(calendar_dates.loc[
        (calendar_dates["date"] == date_int) & (calendar_dates["exception_type"] == 1),
        "service_id"
    ])
    removals = set(calendar_dates.loc[
        (calendar_dates["date"] == date_int) & (calendar_dates["exception_type"] == 2),
        "service_id"
    ])

    return (regular | additions) - removals


def departures_for_date(
    st: pd.DataFrame,
    services: set,
    midnight: datetime,
    now_ts: float,
    horizon_ts: float,
) -> list:
    rows = st[st["service_id"].isin(services)]
    trips = []
    for _, row in rows.iterrows():
        dep_delta = parse_gtfs_time(str(row["departure_time"]))
        dep_dt = midnight + dep_delta
        dep_unix = int(dep_dt.timestamp())

        if dep_unix <= now_ts or dep_unix > horizon_ts:
            continue

        route_name = str(row.get("route_short_name", ""))
        raw_dest = row.get("destination")
        headsign = str(raw_dest) if pd.notna(raw_dest) else ""

        trips.append({
            "routeId":       str(row["route_id"]),
            "routeName":     route_name,
            "routeColor":    ROUTE_COLORS.get(route_name, "888888"),
            "headsign":      headsign,
            "arrivalTime":   dep_unix,
            "departureTime": dep_unix,
            "isRealtime":    False,
        })
    return trips


def main() -> None:
    now_madrid = datetime.now(MADRID_TZ)
    today_str    = now_madrid.strftime("%Y%m%d")
    tomorrow_str = (now_madrid + timedelta(days=1)).strftime("%Y%m%d")
    now_ts       = now_madrid.timestamp()
    horizon_ts   = now_ts + HOURS_AHEAD * 3600

    print("Downloading Renfe GTFS...")
    r = requests.get(GTFS_URL, timeout=60)
    r.raise_for_status()
    print("Download complete, parsing...")

    str_dtype = {"stop_id": str, "route_id": str, "trip_id": str, "service_id": str}

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open("stop_times.txt") as f:
            stop_times = pd.read_csv(f, dtype=str_dtype)
        with z.open("trips.txt") as f:
            trips_df = pd.read_csv(f, dtype=str_dtype)
        with z.open("routes.txt") as f:
            routes_df = pd.read_csv(f, dtype=str_dtype)
        with z.open("stops.txt") as f:
            stops_df = pd.read_csv(f, dtype=str_dtype)

        try:
            with z.open("calendar.txt") as f:
                calendar = pd.read_csv(f, dtype=str_dtype)
            # numeric columns for date comparison
            calendar["start_date"] = calendar["start_date"].astype(int)
            calendar["end_date"]   = calendar["end_date"].astype(int)
            for col in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]:
                calendar[col] = calendar[col].astype(int)
        except KeyError:
            calendar = pd.DataFrame()

        with z.open("calendar_dates.txt") as f:
            cal_dates = pd.read_csv(f, dtype=str_dtype)
        cal_dates["date"]           = cal_dates["date"].astype(int)
        cal_dates["exception_type"] = cal_dates["exception_type"].astype(int)

    # Compute destination name per trip (last stop in sequence)
    last_stop = (
        stop_times.sort_values("stop_sequence")
        .groupby("trip_id")["stop_id"]
        .last()
        .reset_index()
        .rename(columns={"stop_id": "last_stop_id"})
    )
    last_stop = last_stop.merge(
        stops_df[["stop_id", "stop_name"]],
        left_on="last_stop_id", right_on="stop_id", how="left"
    )[["trip_id", "stop_name"]].rename(columns={"stop_name": "destination"})

    # Build joined table for our stop
    st = stop_times[stop_times["stop_id"] == STOP_ID].copy()
    st = st.merge(trips_df[["trip_id", "route_id", "service_id"]], on="trip_id", how="left")
    st = st.merge(routes_df[["route_id", "route_short_name"]],     on="route_id", how="left")
    st = st.merge(last_stop, on="trip_id", how="left")

    today_services    = active_services(calendar, cal_dates, today_str)
    tomorrow_services = active_services(calendar, cal_dates, tomorrow_str)

    midnight_today    = datetime(now_madrid.year, now_madrid.month, now_madrid.day, tzinfo=MADRID_TZ)
    midnight_tomorrow = midnight_today + timedelta(days=1)

    all_trips = (
        departures_for_date(st, today_services,    midnight_today,    now_ts, horizon_ts) +
        departures_for_date(st, tomorrow_services, midnight_tomorrow, now_ts, horizon_ts)
    )

    all_trips.sort(key=lambda x: x["departureTime"])
    all_trips = all_trips[:LIMIT]

    result = {"event": "schedule", "data": {"trips": all_trips}}

    os.makedirs("data", exist_ok=True)
    out_path = "data/renfe-departures.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(all_trips)} departures to {out_path}")
    for t in all_trips:
        dt = datetime.fromtimestamp(t["departureTime"], tz=MADRID_TZ)
        print(f"  {dt.strftime('%H:%M')}  {t['routeName']:10}  → {t['headsign']}")


if __name__ == "__main__":
    main()
