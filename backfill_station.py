"""
One-time: put Richmond airport (KRID) station highs / lows into every existing actual, keeping the
Open-Meteo archive values as om_high / om_low (2026-09-28). New days get this from fetch_weather.py.
"""
import json
from pathlib import Path

import requests

HEADERS = {"User-Agent": "weather-tracker/1.0 (github.com/kwcool87/weather-tracker)"}
path = Path("data") / "actuals.json"
act = json.loads(path.read_text())
months = sorted({d[:7] for d in act})
station = {}
for ym in months:
    y, m = ym.split("-")
    r = requests.get("https://mesonet.agron.iastate.edu/api/1/daily.json",
                     params={"station": "RID", "network": "IN_ASOS", "year": y, "month": int(m)},
                     headers=HEADERS, timeout=60)
    r.raise_for_status()
    for row in r.json().get("data") or []:
        station[row["date"]] = (row.get("max_tmpf"), row.get("min_tmpf"))
n = 0
for d, a in act.items():
    a.setdefault("om_high", a.get("high"))
    a.setdefault("om_low", a.get("low"))
    hi, lo = station.get(d, (None, None))
    if hi is not None and lo is not None:
        a["high"], a["low"], a["temp_source"] = round(hi), round(lo), "KRID"
        n += 1
    else:
        a.setdefault("temp_source", "open_meteo")
path.write_text(json.dumps(act, indent=2))
print(f"station temps on {n} of {len(act)} days")
diffs = [act[d]["high"] - act[d]["om_high"] for d in act if act[d].get("temp_source") == "KRID" and act[d].get("om_high") is not None]
diffl = [act[d]["low"] - act[d]["om_low"] for d in act if act[d].get("temp_source") == "KRID" and act[d].get("om_low") is not None]
if diffs:
    print(f"station minus Open-Meteo archive: highs {sum(diffs) / len(diffs):+.1f} F on average, "
          f"lows {sum(diffl) / len(diffl):+.1f} F")
