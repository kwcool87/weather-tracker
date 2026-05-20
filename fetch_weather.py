"""
Weather Forecast Accuracy Tracker — Daily Fetch Script
Runs via GitHub Actions at 8 AM ET every day.

Forecast sources (all free, no API key required):
  nws        — NWS / Weather.gov       (official NOAA forecast)
  om_gfs     — Open-Meteo / GFS        (US global model)
  om_ecmwf   — Open-Meteo / ECMWF      (European model, global gold standard)
  om_icon    — Open-Meteo / ICON        (German DWD model)
  wttr       — wttr.in                  (aggregated forecast service)

Actuals sources:
  Open-Meteo archive (primary)
  NASA POWER         (secondary validation)
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
    {"id": "nws",      "label": "NWS / Weather.gov"},
    {"id": "om_gfs",   "label": "Open-Meteo GFS"},
    {"id": "om_ecmwf", "label": "Open-Meteo ECMWF"},
    {"id": "om_icon",  "label": "Open-Meteo ICON"},
    {"id": "hrrr",     "label": "NOAA HRRR"},
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
            "date":          dt,
            "high":          days[dt].get("high"),
            "low":           days[dt].get("low"),
            "cloud_cover":   None,
            "precip_prob":   days[dt].get("precip_prob"),
            "precip_amount": None,
        }
        for dt in sorted(days)
        if dt >= log_date
    ]

# ── Open-Meteo shared helper ──────────────────────────────────────────────
def _fetch_open_meteo(log_date, model=None):
    params = {
        "latitude":           LAT,
        "longitude":          LON,
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
        "forecast_days":      10,
    }
    if model:
        params["models"] = model

    r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20)
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

# ── Source 2: Open-Meteo GFS (US model) ──────────────────────────────────
def fetch_om_gfs(log_date):
    return _fetch_open_meteo(log_date, model="gfs_seamless")

# ── Source 3: Open-Meteo ECMWF (European model) ──────────────────────────
def fetch_om_ecmwf(log_date):
    return _fetch_open_meteo(log_date, model="ecmwf_ifs025")

# ── Source 4: Open-Meteo ICON (German DWD model) ─────────────────────────
def fetch_om_icon(log_date):
    return _fetch_open_meteo(log_date, model="icon_seamless")

# ── Source 5: NOAA HRRR via Herbie ───────────────────────────────────────
def fetch_hrrr(log_date):
    """
    NOAA HRRR 3-km CONUS model pulled directly via the Herbie library
    (byte-range GRIB2 from NOAA AWS S3 / NOMADS).

    Variables fetched for each forecast hour 1-48:
      TMP   2 m temperature          (K → °F)
      RH    2 m relative humidity    (%)
      APCP  1-h precipitation        (mm → in, 1-h accumulations)
      DSWRF downward SW radiation    (W/m² → summed to MJ/m²/day)
      UGRD/VGRD  10 m wind components (m/s → mph resultant vector speed)

    Grid point: nearest HRRR 3-km cell to LAT/LON.
    HRRR is a deterministic model — cloud_cover and precip_prob are null.
    Extended agronomic fields are stored alongside the base schema.
    """
    from herbie import FastHerbie, Herbie
    from collections import defaultdict
    from pathlib import Path
    from datetime import timezone
    import numpy as np
    import pandas as pd

    SAVE_DIR = Path("/tmp/hrrr")
    SAVE_DIR.mkdir(exist_ok=True)

    # Most recent 6-h HRRR cycle (00/06/12/18 Z) that is ≥ 2 h old
    # to ensure NOMADS/AWS ingestion is complete.
    now_utc  = datetime.now(timezone.utc)
    run_hour = (now_utc.hour // 6) * 6
    run_dt   = now_utc.replace(hour=run_hour, minute=0, second=0, microsecond=0)
    if (now_utc - run_dt).total_seconds() < 7_200:
        run_dt -= timedelta(hours=6)
    run_str = run_dt.strftime("%Y-%m-%d %H:%M")
    print(f"  HRRR run: {run_str} UTC  fxx 1-48")

    # ── nearest-point extractor ──────────────────────────────────────────
    _grid_idx = None   # cache once the grid is resolved

    def _point(ds):
        """
        Return (pd.DatetimeIndex [UTC-aware], np.float64[]) for our grid point.
        Works for 2-D (single time) or 3-D (multi-time) HRRR fields.
        """
        nonlocal _grid_idx
        if ds is None or not ds.data_vars:
            return pd.DatetimeIndex([], tz="UTC"), np.array([])

        v    = ds[list(ds.data_vars)[0]]
        lats = np.asarray(ds.latitude).ravel()
        lons = np.asarray(ds.longitude).ravel()
        lons = np.where(lons > 180, lons - 360, lons)   # 0-360 → ±180

        if _grid_idx is None:
            _grid_idx = int(((lats - LAT)**2 + (lons - LON)**2).argmin())

        # Resolve valid times as timezone-aware UTC Timestamps
        if "valid_time" in ds.coords:
            raw_vt = np.atleast_1d(ds.valid_time.values)
        else:
            base   = pd.Timestamp(np.atleast_1d(ds.time.values)[0])
            raw_vt = np.array([base + pd.Timedelta(s)
                                for s in np.atleast_1d(ds.step.values)],
                               dtype="datetime64[ns]")
        vt = pd.DatetimeIndex(raw_vt).tz_localize("UTC")

        # Flatten spatial dims → (n_time, n_pts); works for 2-D and 3-D
        arr  = v.values.reshape(-1, lats.size)
        vals = arr[:, _grid_idx].astype(float)
        return vt, vals

    # ── per-hour fetch for a single variable ─────────────────────────────
    def _fetch_hourly(search_template):
        """
        Download one GRIB2 file per forecast hour (1-48) and extract our
        grid point.  `search_template` may contain `{fx}` and `{fx1}`
        placeholders (fx1 = fx-1) for APCP step-range matching.
        Returns {pd.Timestamp: float} mapping.
        """
        data = {}
        for fx in range(1, 49):
            search = search_template.format(fx=fx, fx1=fx - 1)
            try:
                H1  = Herbie(run_str, model="hrrr", product="sfc",
                             fxx=fx, save_dir=str(SAVE_DIR), verbose=False)
                ds1 = H1.xarray(search, remove_grib=True)
                vt1, vals1 = _point(ds1)
                for t, val in zip(vt1, vals1):
                    data[t] = float(val)
            except Exception:
                pass
        return data

    # ── fetch all variables, batch first → per-hour fallback ─────────────
    # Instantaneous fields (TMP, RH, DSWRF, UGRD, VGRD) usually concatenate
    # cleanly via FastHerbie; fall back to per-hour if cfgrib chokes.
    # APCP skips the batch entirely because the idx contains both 1-h
    # incremental and 6-h cumulative messages; we use exact step-range
    # matching ({fx1}-{fx} hour acc) to guarantee 1-h increments only.

    FH = FastHerbie(
        run_str,
        model   = "hrrr",
        product = "sfc",
        fxx     = list(range(1, 49)),
        save_dir= str(SAVE_DIR),
        verbose = False,
    )

    raw: dict = {}
    for key, search in [
        ("tmp",   "TMP:2 m above ground"),
        ("rh",    "RH:2 m above ground"),
        ("dswrf", "DSWRF:surface"),
        ("ugrd",  "UGRD:10 m above ground"),
        ("vgrd",  "VGRD:10 m above ground"),
    ]:
        batch_ok = False
        try:
            ds = FH.xarray(search, remove_grib=True)
            vt, vals = _point(ds)
            if len(vt) >= 24:          # expect at least 24 hours of data
                raw[key] = dict(zip(vt, vals))
                print(f"    {key}: {len(vt)} h OK (batch)")
                batch_ok = True
        except Exception as e:
            print(f"    {key} batch failed ({e}); per-hour fallback…")

        if not batch_ok:
            raw[key] = _fetch_hourly(search)
            print(f"    {key}: {len(raw[key])} h (per-hour fallback)")

    # APCP — HRRR stores precipitation as CUMULATIVE total since run
    # initialization (:APCP:surface:0-N hour acc:), NOT 1-hour increments.
    # Fetch all 48 cumulative values, then diff consecutive entries to
    # recover hourly incremental precipitation.
    print("    apcp: fetching cumulative totals for differencing…")
    apcp_cum = {}
    for fx in range(1, 49):
        try:
            H1   = Herbie(run_str, model="hrrr", product="sfc",
                          fxx=fx, save_dir=str(SAVE_DIR), verbose=False)
            ds_p = H1.xarray(":APCP:surface:", remove_grib=True)
            vt1, vals1 = _point(ds_p)
            for t, val in zip(vt1, vals1):
                apcp_cum[t] = float(val)
        except Exception:
            pass

    # Diff consecutive cumulative values → hourly incremental precip (mm)
    apcp_sorted = sorted(apcp_cum.items())   # list of (Timestamp, mm)
    raw["apcp"] = {}
    for i, (t, cum_val) in enumerate(apcp_sorted):
        prev = apcp_sorted[i - 1][1] if i > 0 else 0.0
        raw["apcp"][t] = max(0.0, cum_val - prev)
    print(f"    apcp: {len(raw['apcp'])} hourly increments (cumulative diff)")

    # ── aggregate hourly → daily in local ET time ─────────────────────────
    all_vt = sorted(set().union(*(set(v.keys()) for v in raw.values())))
    days   = defaultdict(lambda: {
        "temps_f": [], "rh": [], "precip_mm": 0.0,
        "rad_wm2": [], "wind_ms": [],
    })

    for vt in all_vt:
        local_d = (
            vt.tz_convert("America/Indiana/Indianapolis")
              .strftime("%Y-%m-%d")
        )
        if local_d < log_date:
            continue
        day = days[local_d]
        if (t  := raw["tmp"].get(vt))   is not None: day["temps_f"].append(t * 9/5 - 459.67)
        if (rh := raw["rh"].get(vt))    is not None: day["rh"].append(rh)
        if (p  := raw["apcp"].get(vt))  is not None: day["precip_mm"] += max(0.0, p)
        if (s  := raw["dswrf"].get(vt)) is not None: day["rad_wm2"].append(max(0.0, s))
        u  = raw["ugrd"].get(vt)
        vc = raw["vgrd"].get(vt)
        if u is not None and vc is not None:
            day["wind_ms"].append(np.sqrt(u**2 + vc**2))

    MM_TO_IN  = 0.0394
    MS_TO_MPH = 2.237
    RAD_TO_MJ = 3600 / 1_000_000   # W/m² × 1 h → MJ/m²/day

    result = []
    for dt in sorted(days):
        d   = days[dt]
        t_  = d["temps_f"]
        rh_ = d["rh"]
        r_  = d["rad_wm2"]
        w_  = d["wind_ms"]
        result.append({
            "date":                dt,
            "high":                round(max(t_))               if t_  else None,
            "low":                 round(min(t_))               if t_  else None,
            "cloud_cover":         None,   # deterministic model — not output
            "precip_prob":         None,   # deterministic model — no probability
            "precip_amount":       round(d["precip_mm"] * MM_TO_IN, 2),
            # ── native HRRR GRIB2 agronomic fields ───────────────────────
            "shortwave_radiation": round(sum(r_) * RAD_TO_MJ, 2) if r_  else None,
            "wind_speed_max":      round(max(w_) * MS_TO_MPH, 1) if w_  else None,
            "humidity_avg":        round(sum(rh_) / len(rh_))    if rh_ else None,
            "humidity_max":        round(max(rh_))               if rh_ else None,
            "humidity_min":        round(min(rh_))               if rh_ else None,
        })
    return result

FETCH_FNS = {
    "nws":      fetch_nws,
    "om_gfs":   fetch_om_gfs,
    "om_ecmwf": fetch_om_ecmwf,
    "om_icon":  fetch_om_icon,
    "hrrr":     fetch_hrrr,
}

# ── Actuals: Open-Meteo archive (primary) ────────────────────────────────
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
    # Primary: Open-Meteo archive
    r = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude":           LAT,
            "longitude":          LON,
            "start_date":         target_date,
            "end_date":           target_date,
            "daily":              ",".join([
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

    high_r   = round(high)      if high   is not None else None
    low_r    = round(low)       if low    is not None else None
    precip_r = round(precip, 2) if precip is not None else None
    cloud_r  = round(cloud)     if cloud  is not None else None

    actual = {
        "high":          high_r,
        "low":           low_r,
        "cloud_cover":   cloud_r,
        "precip_amount": precip_r,
        "condition":     _cloud_to_condition(cloud_r, precip_r),
    }

    # Secondary: NASA POWER (cross-check, fills in if primary missing)
    try:
        rp = requests.get(
            "https://power.larc.nasa.gov/api/temporal/daily/point",
            params={
                "parameters": "T2M_MAX,T2M_MIN,PRECTOTCORR,CLOUD_AMT",
                "community":  "AG",
                "longitude":  LON,
                "latitude":   LAT,
                "start":      target_date.replace("-", ""),
                "end":        target_date.replace("-", ""),
                "format":     "JSON",
            },
            timeout=30,
        )
        rp.raise_for_status()
        pd = rp.json()["properties"]["parameter"]
        dt_key = target_date.replace("-", "")

        def c_to_f(c):
            return round(c * 9/5 + 32) if c is not None and c != -999 else None

        nasa_high   = c_to_f(pd.get("T2M_MAX", {}).get(dt_key))
        nasa_low    = c_to_f(pd.get("T2M_MIN", {}).get(dt_key))
        nasa_precip = pd.get("PRECTOTCORR", {}).get(dt_key)
        nasa_cloud  = pd.get("CLOUD_AMT",   {}).get(dt_key)

        actual["nasa_high"]   = nasa_high
        actual["nasa_low"]    = nasa_low
        actual["nasa_precip"] = round(float(nasa_precip) * 0.0394, 2) if nasa_precip and float(nasa_precip) != -999 else None
        actual["nasa_cloud"]  = round(float(nasa_cloud)) if nasa_cloud and float(nasa_cloud) != -999 else None

        # Fill primary gaps with NASA data
        if actual["high"]   is None: actual["high"]   = nasa_high
        if actual["low"]    is None: actual["low"]    = nasa_low

    except Exception as e:
        print(f"  NASA POWER actuals failed (non-fatal): {e}")

    return actual

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

    # 1. Fetch forecasts from all sources
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
                entry = {
                    "logged_date":   log_date,
                    "target_date":   day["date"],
                    "source":        src["id"],
                    "lead_days":     days_between(log_date, day["date"]),
                    "high":          day.get("high"),
                    "low":           day.get("low"),
                    "cloud_cover":   day.get("cloud_cover"),
                    "precip_prob":   day.get("precip_prob"),
                    "precip_amount": day.get("precip_amount"),
                }
                # Persist extended agronomic fields (HRRR only — absent on other sources)
                for xk in ("shortwave_radiation", "wind_speed_max",
                            "humidity_max", "humidity_min", "humidity_avg"):
                    if xk in day:
                        entry[xk] = day[xk]
                forecasts.append(entry)
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
