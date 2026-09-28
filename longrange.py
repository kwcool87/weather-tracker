"""
Long-range (weeks 2-8) forecast collection and scoring for the tracker point.

Beyond ~10 days, day-by-day forecasts have no skill; what can have skill is the ODDS that a multi-week
period is below / near / above normal. So long-range sources are stored and scored as tercile
probabilities for fixed windows, against climatology (1/3 each), with the ranked probability skill
score (RPSS: > 0 = better than just assuming normal).

Sources collected daily (data/longrange.json):
  cpc_610 / cpc_814   NOAA CPC 6-10 and 8-14 day outlooks          (days 6-10, 8-14)
  cpc_wk34            NOAA CPC week 3-4 outlook                     (days 15-28)
  cpc_month           NOAA CPC monthly outlook (mid-month + end-of-month update)
  cfs                 CFSv2 seasonal ensemble via Open-Meteo        windows d15-28, d29-42, d31-60
  gfs_ens             GFS ensemble (35 days) via Open-Meteo         windows d15-28, d29-35
CPC gives a favoured category + probability per map polygon; the point's polygon is converted to
below / near / above with CPC's convention. Models store each member's window mean temperature (F) and
total rain (in); the scorer turns members into tercile odds.

Truth and normals (both gridMET at the point, so they're consistent):
  data/climo_gridmet.json   daily Tmax / Tmin / rain 1991-2025 (built once: python longrange.py --climo)
  observations for recent windows are pulled from gridMET's current-year files at scoring time.
Scores: data/longrange_scores.json (by source x window x variable).
"""
import io
import json
import math
import statistics as st
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

DATA_DIR = Path("data")
LR_PATH = DATA_DIR / "longrange.json"
CLIMO_PATH = DATA_DIR / "climo_gridmet.json"
SCORES_PATH = DATA_DIR / "longrange_scores.json"
CPC = "https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst/"
CLIMO_YEARS = (1991, 2020)                     # CPC's normals period
GRIDMET = "http://thredds.northwestknowledge.net:8080/thredds/ncss/MET/{v}/{v}_{y}.nc"
GM_VARS = {"tmmx": ("air_temperature", 0.1, 220.0), "tmmn": ("air_temperature", 0.1, 210.0),
           "pr": ("precipitation_amount", 0.1, 0.0)}
MODEL_WINDOWS = {"cfs": [(15, 28), (29, 42), (31, 60)], "gfs_ens": [(15, 28), (29, 35)]}
HEADERS = {"User-Agent": "weather-tracker/1.0 (github.com/kwcool87/weather-tracker)"}


# ── tercile conversion (CPC convention) ────────────────────────────────────────────────────────────
# CPC maps draw probability BANDS; a polygon's Prob is the band's lower edge (33 = 33-40 %, 40 = 40-50 %,
# 50 = 50-60 % ...). Use the band midpoint as the favoured category's probability.
CPC_BAND_MID = {33: 36.5, 40: 45.0, 50: 55.0, 60: 65.0, 70: 75.0, 80: 85.0, 90: 95.0}


def cpc_terciles(cat, prob):
    """(p_below, p_near, p_above) from CPC's favoured category and its probability band (percent)."""
    pr = round(prob) if prob else 33
    p = CPC_BAND_MID.get(pr, pr) / 100.0
    c = (cat or "EC").strip().lower()
    if c.startswith("n"):                         # near normal favoured
        return ((1 - p) / 2, p, (1 - p) / 2)
    if c.startswith("a") or c.startswith("b"):
        opp = 2 / 3 - p if p <= 0.633 else 0.033
        near = 1 - p - opp
        return (opp, near, p) if c.startswith("a") else (p, near, opp)
    return (1 / 3, 1 / 3, 1 / 3)                  # EC = equal chances


# ── CPC shapefile point lookup (pure python; pyshp) ────────────────────────────────────────────────
def _in_ring(x, y, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def cpc_point(zbytes, lat, lon):
    """Attributes of the polygon containing the point, from a zipped CPC shapefile."""
    import shapefile
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    base = [n[:-4] for n in z.namelist() if n.lower().endswith(".shp")][0]
    r = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")),
                         shx=io.BytesIO(z.read(base + ".shx")))
    fields = [f[0] for f in r.fields[1:]]
    for sr in r.iterShapeRecords():
        sh = sr.shape
        x0, y0, x1, y1 = sh.bbox
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        parts = list(sh.parts) + [len(sh.points)]
        inside = False
        for a, b in zip(parts, parts[1:]):
            if _in_ring(lon, lat, [tuple(p) for p in sh.points[a:b]]):
                inside = not inside              # even-odd: holes cancel
        if inside:
            return dict(zip(fields, sr.record))
    return None


def _d(v):
    if isinstance(v, date):
        return v.isoformat()
    s = str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _month_bounds(valid):
    """'Oct 2026' -> ('2026-10-01', '2026-10-31')."""
    m = datetime.strptime(valid.strip(), "%b %Y").date()
    nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return m.isoformat(), (nxt - timedelta(days=1)).isoformat()


def fetch_cpc(log_date, lat, lon):
    out = []
    today = date.fromisoformat(log_date)
    for var in ("temp", "prcp"):
        cands = {"cpc_610": [f"610{var}_{(today - timedelta(days=k)).strftime('%Y%m%d')}.zip" for k in (0, 1, 2)],
                 "cpc_814": [f"814{var}_{(today - timedelta(days=k)).strftime('%Y%m%d')}.zip" for k in (0, 1, 2)],
                 "cpc_wk34": [f"wk34{var}_latest.zip"],
                 "cpc_month": [f"monthupd_{var}_latest.zip"]}
        for prod, names in cands.items():
            for nm in names:
                try:
                    r = requests.get(CPC + nm, headers=HEADERS, timeout=120)
                    if r.status_code != 200 or r.content[:2] != b"PK":
                        continue
                    a = cpc_point(r.content, lat, lon)
                    if not a:
                        continue
                    if "Valid_Seas" in a:
                        start, end = _month_bounds(a["Valid_Seas"])
                    else:
                        start, end = _d(a.get("Start_Date")), _d(a.get("End_Date"))
                    pb, pn, pa = cpc_terciles(a.get("Cat"), a.get("Prob"))
                    out.append({"kind": "cpc", "source": prod, "var": var, "issued": _d(a.get("Fcst_Date")),
                                "logged": log_date, "start": start, "end": end, "cat": a.get("Cat"),
                                "prob": a.get("Prob"), "p": [round(pb, 3), round(pn, 3), round(pa, 3)]})
                    break
                except Exception as e:
                    print(f"  CPC {nm}: {e}")
    return out


# ── model ensembles via Open-Meteo ─────────────────────────────────────────────────────────────────
def _members(daily, key):
    ks = [k for k in daily if k == key or k.startswith(key + "_member")]
    return [daily[k] for k in sorted(ks)]


def fetch_models(log_date, lat, lon):
    out = []
    today = date.fromisoformat(log_date)
    calls = {
        "cfs": ("https://seasonal-api.open-meteo.com/v1/seasonal",
                {"daily": "temperature_2m_max,temperature_2m_min,precipitation_sum", "forecast_days": 62}),
        "gfs_ens": ("https://ensemble-api.open-meteo.com/v1/ensemble",
                    {"daily": "temperature_2m_mean,precipitation_sum", "models": "gfs_seamless", "forecast_days": 35}),
    }
    for src, (url, extra) in calls.items():
        try:
            r = requests.get(url, params={"latitude": lat, "longitude": lon, **extra}, timeout=120)
            r.raise_for_status()
            d = r.json()["daily"]
            days = [date.fromisoformat(t) for t in d["time"]]
            if src == "cfs":
                tx, tn = _members(d, "temperature_2m_max"), _members(d, "temperature_2m_min")
                tm = [[(a + b) / 2 if a is not None and b is not None else None for a, b in zip(x, n)]
                      for x, n in zip(tx, tn)]
            else:
                tm = _members(d, "temperature_2m_mean")
            pr = _members(d, "precipitation_sum")
            for w0, w1 in MODEL_WINDOWS[src]:
                s, e = today + timedelta(days=w0), today + timedelta(days=w1)
                idx = [i for i, t in enumerate(days) if s <= t <= e]
                if len(idx) < (w1 - w0 + 1):
                    continue
                tmem = [round(st.mean(m[i] for i in idx) * 9 / 5 + 32, 2) for m in tm
                        if all(m[i] is not None for i in idx)]
                pmem = [round(sum(m[i] for i in idx) / 25.4, 2) for m in pr if all(m[i] is not None for i in idx)]
                if len(tmem) < 3 or len(pmem) < 3:
                    continue          # window not fully covered by this run
                out.append({"kind": "model", "source": src, "issued": log_date, "logged": log_date,
                            "window": f"d{w0}-{w1}", "start": s.isoformat(), "end": e.isoformat(),
                            "temp_members": tmem, "prcp_members": pmem})
        except Exception as e:
            print(f"  {src}: {e}")
    return out


def collect(log_date, lat, lon):
    """Daily: append today's long-range forecasts to data/longrange.json (deduplicated)."""
    DATA_DIR.mkdir(exist_ok=True)
    lr = json.loads(LR_PATH.read_text()) if LR_PATH.exists() else []
    seen = {(x["kind"], x["source"], x.get("var"), x.get("window"), x["issued"], x["start"]) for x in lr}
    new = [x for x in fetch_cpc(log_date, lat, lon) + fetch_models(log_date, lat, lon)
           if (x["kind"], x["source"], x.get("var"), x.get("window"), x["issued"], x["start"]) not in seen]
    lr += new
    LR_PATH.write_text(json.dumps(lr, indent=1))
    print(f"  long-range: {len(new)} new entries ({len(lr)} total)")
    return new


# ── gridMET truth + normals ────────────────────────────────────────────────────────────────────────
def gridmet_year(lat, lon, y, last=None):
    """{date: (tmax_F, tmin_F, rain_in)} for one year at the point."""
    got = {}
    for v, (var, scale, off) in GM_VARS.items():
        end = last or f"{y}-12-31"
        url = (GRIDMET.format(v=v, y=y) + f"?var={var}&latitude={lat:.4f}&longitude={lon:.4f}"
               f"&time_start={y}-01-01T00:00:00Z&time_end={end}T00:00:00Z&accept=csv")
        for attempt in range(6):
            try:
                t = requests.get(url, timeout=180).text
                break
            except Exception:
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"gridMET {v} {y} unavailable")
        for line in t.splitlines()[1:]:
            c = line.split(",")
            if len(c) < 2 or c[-1] in ("", "NaN"):
                continue
            val = float(c[-1]) * scale + off
            got.setdefault(c[0][:10], {})[v] = val
    out = {}
    for d0, x in got.items():
        if len(x) == 3:
            out[d0] = (round((x["tmmx"] - 273.15) * 9 / 5 + 32, 1), round((x["tmmn"] - 273.15) * 9 / 5 + 32, 1),
                       round(x["pr"] / 25.4, 3))
    return out


def build_climo(lat, lon, y0=1991, y1=2025):
    days = {}
    for y in range(y0, y1 + 1):
        days.update(gridmet_year(lat, lon, y))
        print(f"  climo {y}: {len(days)} days", flush=True)
    DATA_DIR.mkdir(exist_ok=True)
    CLIMO_PATH.write_text(json.dumps({"source": "gridMET at the tracker point", "lat": lat, "lon": lon,
                                      "days": days}))


# ── scoring ────────────────────────────────────────────────────────────────────────────────────────
def _window_stat(days, start, end, var):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    vals = []
    t = s
    while t <= e:
        x = days.get(t.isoformat())
        if x is None:
            return None
        vals.append((x[0] + x[1]) / 2 if var == "temp" else x[2])
        t += timedelta(days=1)
    return st.mean(vals) if var == "temp" else sum(vals)


def _climo_terciles(days, start, end, var):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    xs = []
    for y in range(CLIMO_YEARS[0], CLIMO_YEARS[1] + 1):
        try:
            ss = s.replace(year=y)
        except ValueError:
            ss = s.replace(year=y, day=28)
        v = _window_stat(days, ss.isoformat(), (ss + (e - s)).isoformat(), var)
        if v is not None:
            xs.append(v)
    if len(xs) < 20:
        return None
    q = st.quantiles(xs, n=3)
    return q[0], q[1]


def _rps(p, obs_cat):
    o = [1.0 if k == obs_cat else 0.0 for k in range(3)]
    cp, co = [p[0], p[0] + p[1]], [o[0], o[0] + o[1]]
    return sum((a - b) ** 2 for a, b in zip(cp, co)) / 2


def score(lat, lon):
    if not CLIMO_PATH.exists() or not LR_PATH.exists():
        print("  scoring skipped (no climatology or no long-range data yet)")
        return None
    days = {k: tuple(v) for k, v in json.loads(CLIMO_PATH.read_text())["days"].items()}
    lr = json.loads(LR_PATH.read_text())
    ends = [date.fromisoformat(x["end"]) for x in lr]
    if not ends:
        return None
    for y in sorted({e.year for e in ends if e.year > 2025}):
        try:
            days.update({k: v for k, v in gridmet_year(lat, lon, y, last=min(date.today(), date(y, 12, 31)).isoformat()).items()})
        except Exception as e:
            print(f"  gridMET {y}: {e}")
    agg = {}
    for x in lr:
        for var in (("temp", "prcp") if x["kind"] == "model" else (x["var"],)):
            obs = _window_stat(days, x["start"], x["end"], var)
            terc = _climo_terciles(days, x["start"], x["end"], var)
            if obs is None or terc is None:
                continue
            cat = 0 if obs < terc[0] else (2 if obs > terc[1] else 1)
            if x["kind"] == "cpc":
                p = x["p"]
                key = (x["source"], "window", var)
            else:
                mem = x[f"{var}_members"]
                if len(mem) < 3:
                    continue
                p = [sum(m < terc[0] for m in mem) / len(mem), 0, sum(m > terc[1] for m in mem) / len(mem)]
                p[1] = 1 - p[0] - p[2]
                key = (x["source"], x["window"], var)
            a = agg.setdefault(key, {"n": 0, "rps": 0.0, "rps_clim": 0.0, "hits": 0, "calls": 0})
            a["n"] += 1
            a["rps"] += _rps(p, cat)
            a["rps_clim"] += _rps([1 / 3, 1 / 3, 1 / 3], cat)
            if max(p) > 0.34:
                a["calls"] += 1
                a["hits"] += int(p.index(max(p)) == cat)
    rows = []
    for (src, win, var), a in sorted(agg.items()):
        rows.append({"source": src, "window": win, "var": var, "n": a["n"],
                     "rpss": round(1 - a["rps"] / a["rps_clim"], 3) if a["rps_clim"] else None,
                     "calls": a["calls"], "hit_rate": round(a["hits"] / a["calls"], 3) if a["calls"] else None})
    SCORES_PATH.write_text(json.dumps({"updated": datetime.now().isoformat(timespec="seconds"),
                                       "note": "RPSS > 0 = better than climatology (1/3 each); hit rate = favoured "
                                               "category came true, when a category was favoured",
                                       "rows": rows}, indent=1))
    for r in rows:
        print(f"  {r['source']:<10} {r['window']:<8} {r['var']:<5} n={r['n']:>4}  RPSS {r['rpss']}  "
              f"hit {r['hit_rate']} of {r['calls']} calls")
    return rows


if __name__ == "__main__":
    LAT, LON = 39.9499, -84.9385
    if "--climo" in sys.argv:
        build_climo(LAT, LON)
    elif "--score" in sys.argv:
        score(LAT, LON)
    else:
        collect(date.today().isoformat(), LAT, LON)
