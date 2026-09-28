"""
One-time: add archived NOAA CPC outlooks for the tracker point to data/longrange.json (2026-09-28).
  week 3-4 (wk34temp / wk34prcp_YYYYMMDD.zip)   issued Apr-Oct of BACKFILL_YEARS
  monthly end-of-month update (monthupd_*_YYYYMM.zip)   everything in the archive (since 2024-04)
Each ~3 MB zip is read at the point and discarded. Entries carry "backfilled": true.
Usage: python backfill_cpc.py
"""
import json
import re
import sys
from datetime import date

import requests

import longrange as L

LAT, LON = 39.9499, -84.9385
BACKFILL_YEARS = (2025, 2026)
MONTHS = range(4, 11)            # issued April - October (growing season)


def main():
    listing = requests.get(L.CPC, headers=L.HEADERS, timeout=120).text
    names = sorted(set(re.findall(r'href="((?:wk34(?:temp|prcp)_\d{8}|monthupd_(?:temp|prcp)_\d{6})\.zip)"', listing)))
    only = set(sys.argv[1:])                 # optional: re-read just these files
    todo = []
    for n in names:
        if only and n not in only:
            continue
        if n.startswith("wk34"):
            d = date(int(n[9:13]), int(n[13:15]), int(n[15:17]))
            if d.year in BACKFILL_YEARS and d.month in MONTHS:
                todo.append(n)
        else:
            todo.append(n)
    lr = json.loads(L.LR_PATH.read_text()) if L.LR_PATH.exists() else []
    seen = {(x["source"], x.get("var"), x["issued"], x["start"]) for x in lr if x["kind"] == "cpc"}
    print(f"  {len(todo)} archived CPC files to read")
    added = 0
    for i, n in enumerate(todo, 1):
        var = "temp" if "temp" in n else "prcp"
        prod = "cpc_wk34" if n.startswith("wk34") else "cpc_month"
        try:
            r = requests.get(L.CPC + n, headers=L.HEADERS, timeout=180)
            if r.status_code != 200 or r.content[:2] != b"PK":
                print(f"  {n}: not available"); continue
            a = L.cpc_point(r.content, LAT, LON)
            if not a:
                a = L.cpc_ec_record(r.content)       # no polygon at the point = Equal Chances
                if not a:
                    print(f"  {n}: empty file"); continue
            if "Valid_Seas" in a:
                start, end = L._month_bounds(a["Valid_Seas"])
            else:
                start, end = L._d(a.get("Start_Date")), L._d(a.get("End_Date"))
            issued = L._d(a.get("Fcst_Date"))
            if (prod, var, issued, start) in seen:
                continue
            pb, pn, pa = L.cpc_terciles(a.get("Cat"), a.get("Prob"))
            lr.append({"kind": "cpc", "source": prod, "var": var, "issued": issued, "logged": issued,
                       "start": start, "end": end, "cat": a.get("Cat"), "prob": a.get("Prob"),
                       "p": [round(pb, 3), round(pn, 3), round(pa, 3)], "backfilled": True})
            seen.add((prod, var, issued, start))
            added += 1
        except Exception as e:
            print(f"  {n}: {type(e).__name__}: {e}")
        if i % 20 == 0:
            L.LR_PATH.write_text(json.dumps(lr, indent=1))
            print(f"  {i}/{len(todo)} read, {added} added", flush=True)
    L.LR_PATH.write_text(json.dumps(lr, indent=1))
    print(f"  done: {added} backfilled CPC outlooks ({len(lr)} long-range entries)")


if __name__ == "__main__":
    main()
