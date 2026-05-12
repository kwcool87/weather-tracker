"""
Weather Forecast Accuracy Tracker — Daily Fetch Script
Runs via GitHub Actions at 8 AM ET every day.

Sources (all free, no API key required):
  nws        — NWS / Weather.gov  (official NOAA forecast)
  open_meteo — Open-Meteo         (free global weather model)
  wttr       — wttr.in            (aggregated forecast service)
"""

import json
import os
import requests
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

ET  = ZoneInfo("America/Indiana/Indianapolis")
LAT = 39.9499
LON = -84.9385
ZIP = "47341"
HEADERS = {"User-Agent": "weather-tracker/1.0 (github.com/kwcool87/weather-tracker)"}

SOURCES = [
    {"id": "nws",        "label": "NWS / Weather.gov"},
    {"id": "open_meteo", "label": "Open-Meteo"},
    {"id": "wttr",       "label": "wttr.in"},
]

def today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")

def yesterday_et():
    return (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")

def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days

# ── Source 1: NWS (Weather.gov) ───────────────────────────────────────────
def fetch_nws(log_date):
    r = requests.get(
        f"https://api.weather.gov/points/{LAT},{LON}",
        headers=HEADERS, timeout=20
    )
    r.raise_for_status()
    props = r.json()["properties"]
    office, gx, gy = props["gridId"], props["gridX"], props["gridY"]

    r = requests.get(
        f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/forecast",
        headers=HEADERS, timeout=20
    )
    r.raise_for_status()
    periods = r.json()["properties"]["periods"]

    days = {}
    for p in periods:
        dt = p["startTime"][:10]
        if dt not in days:
            days[dt] = {}
        prob = (p.get("probabilityOfPrecipitation") or {}).get("value")
        if p["isDaytime"]:
            days[dt]["high"] = p["temperature"]
            days[dt]["precip_prob"] = prob
        else:
            days[dt]["low"] = p["temperature"]
            if "precip_prob" not in days[dt]:
                days[dt]["precip_prob"] = prob

    return [
        {
            "date": dt,
            "high": days[dt].get("high"),
            "low":  days[dt].get("low"),
            "cloud_cover":   None,
            "precip_prob":   days[dt].get("precip_prob"),
            "precip_amount": None,
        }
        for dt in sorted(days)
        if dt >= log_date
    ]

# ── Source 2: Open-Meteo ──────────────────────────────────────────────────
def fetch_open_meteo(log_date):
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  LAT,
            "longitude": LON,
            "daily": ",".join([
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "cloud_cover_mean",
            ]),
            "temperature_unit":   "fahrenheit",
            "precipitation_unit": "inch",
            "timezone":           "America/Indiana/Indianapolis",
            "forecast_days":      10,
        },
        timeout=20,
    )
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
        if dt >= log_date
    ]

# ── Source 3: wttr.in ─────────────────────────────────────────────────────
def fetch_wttr(log_date):
    r = requests.get(
        f"https://wttr.in/{ZIP}?format=j1",
        headers={"User-Agent": "curl/7.68.0"},
        timeout=20,
    )
    r.raise_for_status()
    weather = r.json().get("weather", [])

    result = []
    base = date.fromisoformat(log_date)
    for i, day in enumerate(weather):
        dt = str(base + timedelta(days=i))
        hourly = day.get("hourly", [])
        avg_cloud = (
            round(sum(int(h.get("cloudcover", 0)) for h in hourly) / len(hourly))
            if hourly else None
        )
        max_precip_prob = (
            max(int(h.get("chanceofrain", 0)) for h in hourly)
            if hourly else None
        )
        total_precip = (
            round(sum(float(h.get("precipMM", 0)) for h in hourly) * 0.0394, 2)
            if hourly else None
        )
        result.append({
            "date":          dt,
            "high":          round(float(day["maxtempF"])),
            "low":           round(float(day["mintempF"])),
            "cloud_cover":   avg_cloud,
            "precip_prob":   max_precip_prob,
            "precip_amount": total_precip,
        })
    return result

FETCH_FNS = {
    "nws":        fetch_nws,
    "open_meteo": fetch_open_meteo,
    "wttr":       fetch_wttr,
}

# ── Actuals: Open-Meteo archive ───────────────────────────────────────────
def _cloud_to_condition(cloud_cover, precip_amount):
    if precip_amount and precip_amount > 0.01:
        return "Rain"
    if cloud_cover is None:
        return "Unknown"
    if cloud_cover < 20:  return "Clear"
    if cloud_cover < 40:  return "Mostly Clear"
    if cloud_cover < 60:  return "Partly Cloudy"
    if cloud_cover < 80:  return "Mostly Cloudy"
    return "Overcast"

def fetch_actual(target_date):
    r = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude":           LAT,
            "longitude":          LON,
            "start_date":         target_date,
            "end_date":           target_date,
            "daily": ",".join([
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "cloud_cover_mean",
            ]),
            "temperature_unit":   "fahrenheit",
            "precipitation_unit": "inch",
            "timezone":           "America/Indiana/Indianapolis",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()["daily"]
    if not d.get("time"):
        raise ValueError("No archive data returned")

    high   = d["temperature_2m_max"][0]
    low    = d["temperature_2m_min"][0]
    precip = d["precipitation_sum"][0]
    cloud  = d["cloud_cover_mean"][0]

    high_r   = round(high)   if high   is not None else None
    low_r    = round(low)    if low    is not None else None
    precip_r = round(precip, 2) if precip is not None else None
    cloud_r  = round(cloud)  if cloud  is not None else None

    return {
        "high":          high_r,
        "low":           low_r,
        "cloud_cover":   cloud_r,
        "precip_amount": precip_r,
        "condition":     _cloud_to_condition(cloud_r, precip_r),
    }

# ── Data persistence ──────────────────────────────────────────────────────
DATA_DIR = Path("data")

def load_data():
    DATA_DIR.mkdir(exist_ok=True)
    fx_path = DATA_DIR / "forecasts.json"
    ac_path = DATA_DIR / "actuals.json"
    forecasts = json.loads(fx_path.read_text()) if fx_path.exists() else []
    actuals   = json.loads(ac_path.read_text()) if ac_path.exists() else {}
    return forecasts, actuals

def save_data(forecasts, actuals):
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "forecasts.json").write_text(json.dumps(forecasts, indent=2))
    (DATA_DIR / "actuals.json").write_text(json.dumps(actuals, indent=2))
    meta = {
        "last_run":               datetime.now(ET).isoformat(),
        "total_forecast_entries": len(forecasts),
        "total_actuals":          len(actuals),
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2))

# ── ntfy.sh notification ──────────────────────────────────────────────────
def send_notification(title, message, ntfy_topic):
    if not ntfy_topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{ntfy_topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "default", "Tags": "partly_sunny"},
            timeout=10,
        )
    except Exception as e:
        print(f"Notification failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    log_date  = today_et()
    yesterday = yesterday_et()
    ntfy_topic = os.environ.get("NTFY_TOPIC", "")

    print(f"=== Weather Fetch: {log_date} ===\n")

    forecasts, actuals = load_data()
    results = {"fetched": {}, "errors": [], "actual": None}

    # 1. Fetch forecasts from all 3 sources
    for src in SOURCES:
        print(f"Fetching {src['label']}...")
        try:
            days = FETCH_FNS[src["id"]](log_date)
            if not days:
                raise ValueError("Empty response")
            print(f"  OK: {len(days)} days")

            forecasts = [
                f for f in forecasts
                if not (f["logged_date"] == log_date and f["source"] == src["id"])
            ]
            for day in days:
                forecasts.append({
                    "logged_date":   log_date,
                    "target_date":   day["date"],
                    "source":        src["id"],
                    "lead_days":     days_between(log_date, day["date"]),
                    "high":          day.get("high"),
                    "low":           day.get("low"),
                    "cloud_cover":   day.get("cloud_cover"),
                    "precip_prob":   day.get("precip_prob"),
                    "precip_amount": day.get("precip_amount"),
                })
            results["fetched"][src["id"]] = len(days)

        except Exception as e:
            print(f"  ERROR: {e}")
            results["errors"].append(f"{src['label']}: {e}")

    # 2. Fetch yesterday's actuals
    print(f"\nFetching actuals for {yesterday}...")
    try:
        act = fetch_actual(yesterday)
        actuals[yesterday] = act
        print(f"  OK: H:{act.get('high')}° / L:{act.get('low')}° — {act.get('condition','')}")
        results["actual"] = act
    except Exception as e:
        print(f"  ERROR: {e}")
        results["errors"].append(f"Actuals ({yesterday}): {e}")

    # 3. Save
    save_data(forecasts, actuals)
    print(f"\nSaved: {len(forecasts)} forecast entries, {len(actuals)} actuals")

    # 4. Notify
    src_lines = [
        f"  {src['label']}: " +
        (f"OK {results['fetched'][src['id']]} days" if src["id"] in results["fetched"] else "FAILED")
        for src in SOURCES
    ]
    act = results["actual"]
    act_line = (
        f"  {yesterday}: H:{act.get('high')}°/L:{act.get('low')}° {act.get('condition','')}"
        if act else f"  {yesterday}: not found"
    )
    err_line = (
        f"\n{len(results['errors'])} error(s):\n" + "\n".join(f"  {e}" for e in results["errors"])
        if results["errors"] else ""
    )
    message = "Forecasts logged for " + log_date + ":\n" + "\n".join(src_lines) + "\n\nActuals:\n" + act_line + err_line
    send_notification(f"Weather Tracker Updated — {log_date}", message, ntfy_topic)
    print(f"\n{message}\n=== Done ===")


if __name__ == "__main__":
    main()
