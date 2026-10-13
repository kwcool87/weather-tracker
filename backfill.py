"""
One-off backfill script for missed scheduled runs.

Fills in two kinds of gaps for the given --date(s):
  1. ACTUALS for each date (if missing) — uses the same fetch_actual()
     the main workflow uses: Open-Meteo archive + KRID ASOS + MRMS QPE +
     NASA POWER gap-fill. These are the authoritative daily observations
     and fully recoverable from the archive.

  2. FORECASTS logged on each date for the Open-Meteo sources
     (GFS, ECMWF, ICON) via Open-Meteo's historical-forecast-api, which
     serves the forecast values the models produced at each timestamp
     (short-lead for the recent past; multi-day forecasts further back).
     Each backfilled row is tagged `"backfill": true` so downstream
     analysis can distinguish them from forecasts captured in real time.

NOT backfilled (data is simply gone):
  - NWS (no public archive of past text forecasts)
  - NOAA HRRR (short retention window on NOMADS/AWS)
  - BAM Weather (no archive endpoint)

Idempotent: re-running against a date that already has backfilled rows
replaces them; actuals already present are left alone.

Usage:
  python backfill.py --date 2026-10-09 --date 2026-10-10
"""

import argparse
import json
import time
from datetime import date

import requests

from fetch_weather import (
    LAT, LON, HEADERS,
    load_data, save_data, days_between,
    fetch_actual,
)

OPEN_METEO_HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"

MODELS = [
    ("om_gfs",   "gfs_seamless"),
    ("om_ecmwf", "ecmwf_ifs025"),
    ("om_icon",  "icon_seamless"),
]


def fetch_hist_forecast(logged_date: str, model_param: str):
    """
    Return a list of day-dicts shaped like _fetch_open_meteo(), for the
    10-day window starting at `logged_date`, as the model's historical
    archive shows them.
    """
    end = (date.fromisoformat(logged_date).toordinal() + 9)
    end_date = date.fromordinal(end).isoformat()
    params = {
        "latitude":           LAT,
        "longitude":          LON,
        "start_date":         logged_date,
        "end_date":           end_date,
        "daily":              ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "cloud_cover_mean",
        ]),
        "temperature_unit":   "fahrenheit",
        "precipitation_unit": "inch",
        "timezone":           "America/Indiana/Indianapolis",
        "models":             model_param,
    }
    r = requests.get(OPEN_METEO_HIST, params=params, headers=HEADERS, timeout=90)
    r.raise_for_status()
    d = r.json()["daily"]
    n = len(d["time"])

    def iv(key, i):
        v = d.get(key, [None] * n)[i]
        return round(v) if v is not None else None

    def fv(key, i):
        v = d.get(key, [None] * n)[i]
        return round(v, 2) if v is not None else None

    return [
        {
            "date":          dt,
            "high":          iv("temperature_2m_max", i),
            "low":           iv("temperature_2m_min", i),
            "cloud_cover":   iv("cloud_cover_mean", i),
            "precip_prob":   iv("precipitation_probability_max", i),
            "precip_amount": fv("precipitation_sum", i),
        }
        for i, dt in enumerate(d["time"])
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append", required=True,
                    help="ISO date (YYYY-MM-DD) to backfill; repeat for multiple")
    args = ap.parse_args()

    forecasts, actuals = load_data()
    added_fx, replaced_fx, added_ac, skipped_ac = 0, 0, 0, 0

    for log_date in args.date:
        print(f"\n=== Backfilling {log_date} ===")

        # --- Actuals (as a target date; always backfill if missing) ------
        if log_date in actuals:
            print(f"  actuals: already present, skipped")
            skipped_ac += 1
        else:
            try:
                act = fetch_actual(log_date)
                actuals[log_date] = act
                print(f"  actuals: H:{act.get('high')}° L:{act.get('low')}° — {act.get('condition','')}")
                added_ac += 1
            except Exception as e:
                print(f"  actuals FAILED: {e}")

        # --- Forecasts (as logged_date for Open-Meteo sources only) -----
        for src_id, model_param in MODELS:
            # Safety: never clobber real-time rows. If any existing row for
            # this (logged_date, source) is NOT already backfill-tagged, skip
            # this source entirely — the real-time fetch is authoritative.
            existing = [f for f in forecasts
                        if f["logged_date"] == log_date and f["source"] == src_id]
            if existing and any(not r.get("backfill") for r in existing):
                print(f"  {src_id}: skipped (real-time rows exist; refusing to overwrite)")
                continue

            # Try once, then retry once on network error (Open-Meteo's
            # historical API has been flaky on back-to-back calls)
            days = None
            last_err = None
            for attempt in (1, 2):
                try:
                    days = fetch_hist_forecast(log_date, model_param)
                    break
                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError) as e:
                    last_err = e
                    if attempt == 1:
                        print(f"  {src_id}: network error, retrying in 10s… ({e})")
                        time.sleep(10)
                except Exception as e:
                    last_err = e
                    break
            if days is None:
                print(f"  {src_id} FAILED: {last_err}")
                continue

            try:
                if not days:
                    raise ValueError("Empty historical response")

                # Drop existing backfill rows for (logged_date, source) — safe
                # now because the guard above ensured none are real-time
                before = len(forecasts)
                forecasts = [
                    f for f in forecasts
                    if not (f["logged_date"] == log_date and f["source"] == src_id)
                ]
                dropped = before - len(forecasts)
                if dropped:
                    replaced_fx += dropped

                for day in days:
                    entry = {
                        "logged_date":   log_date,
                        "target_date":   day["date"],
                        "source":        src_id,
                        "lead_days":     days_between(log_date, day["date"]),
                        "high":          day.get("high"),
                        "low":           day.get("low"),
                        "cloud_cover":   day.get("cloud_cover"),
                        "precip_prob":   day.get("precip_prob"),
                        "precip_amount": day.get("precip_amount"),
                        "backfill":      True,
                    }
                    forecasts.append(entry)
                    added_fx += 1
                print(f"  {src_id}: {len(days)} rows (backfill)")
            except Exception as e:
                print(f"  {src_id} FAILED: {e}")

    save_data(forecasts, actuals, update_last_run=False)
    print(f"\nSaved: +{added_fx} forecast rows ({replaced_fx} replaced), +{added_ac} actuals ({skipped_ac} skipped)")
    print(f"Totals: {len(forecasts)} forecast entries, {len(actuals)} actuals")


if __name__ == "__main__":
    main()
