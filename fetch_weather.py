"""
Weather Forecast Accuracy Tracker — Daily Fetch Script
Runs via GitHub Actions at 8 AM ET every day.
Fetches forecasts from Weather Underground, Weather.com, and AccuWeather
for ZIP 47341 (Hagerstown, IN), then fetches yesterday's actuals.
"""

import anthropic
import json
import os
import re
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Timezone ───────────────────────────────────────────────────────────────
ET = ZoneInfo("America/Indiana/Indianapolis")

def today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")

def yesterday_et():
    return (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")

def days_between(a, b):
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days

# ── Claude API with web search ─────────────────────────────────────────────
def call_claude(prompt, max_tokens=2000):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if hasattr(b, "text"))

def extract_json(text):
    text = re.sub(r"```json|```", "", text).strip()
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try: return json.loads(m.group())
        except: pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try: return json.loads(m.group())
        except: pass
    return json.loads(text)

# ── Weather sources ────────────────────────────────────────────────────────
SOURCES = [
    {"id": "wunderground", "label": "Weather Underground", "site": "wunderground.com"},
    {"id": "weather_com",  "label": "Weather.com",          "site": "weather.com"},
    {"id": "accuweather",  "label": "AccuWeather",           "site": "accuweather.com"},
]

def fetch_forecast(source, log_date):
    prompt = (
        f"Search {source['site']} for the current 10-day weather forecast for ZIP code 47341 "
        f"Fountain City Indiana. Today is {log_date}. "
        f"Return ONLY a raw JSON array, no markdown, no explanation: "
        f'[{{"date":"YYYY-MM-DD","high":75,"low":58,"cloud_cover":40,"precip_prob":20,"precip_amount":0.1}}] '
        f"Rules: dates YYYY-MM-DD starting {log_date}. high/low = °F integers. "
        f"cloud_cover = sky cover 0-100 int (null if not listed). precip_prob = 0-100 int. "
        f"precip_amount = inches decimal (null if not listed). ONLY the JSON array, nothing else."
    )
    raw = call_claude(prompt, max_tokens=2000)
    data = extract_json(raw)
    if not isinstance(data, list) or not data:
        raise ValueError("Empty or invalid forecast array")
    return [d for d in data if d.get("date") and (d.get("high") is not None or d.get("low") is not None)]

def fetch_actual(date):
    prompt = (
        f"Search for actual observed weather conditions in Fountain City Indiana ZIP 47341 on {date}. "
        f"Check wunderground.com/history, weather.gov, or climate data sources. "
        f"Return ONLY a raw JSON object, no markdown: "
        f'{{"high":75,"low":58,"cloud_cover":40,"precip_amount":0.12,"condition":"Partly Cloudy"}} '
        f"high/low = actual max/min °F integers. cloud_cover = avg sky cover % (null if unknown). "
        f"precip_amount = total precipitation inches, 0.0 if none fell. "
        f"condition = one of: Clear, Mostly Clear, Partly Cloudy, Mostly Cloudy, Overcast, Rain, Thunderstorms, Snow. "
        f"ONLY the JSON object."
    )
    raw = call_claude(prompt, max_tokens=600)
    obj = extract_json(raw)
    if not isinstance(obj, dict) or isinstance(obj, list):
        raise ValueError("Not a JSON object")
    return obj

# ── Data persistence ───────────────────────────────────────────────────────
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

    # Also write a "last_updated" metadata file for the dashboard
    meta = {
        "last_run": datetime.now(ET).isoformat(),
        "total_forecast_entries": len(forecasts),
        "total_actuals": len(actuals),
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2))

# ── ntfy.sh notification ───────────────────────────────────────────────────
def send_notification(title, message, ntfy_topic):
    if not ntfy_topic:
        print("NTFY_TOPIC not set — skipping push notification")
        return
    try:
        r = requests.post(
            f"https://ntfy.sh/{ntfy_topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "default",
                "Tags": "partly_sunny,white_check_mark",
            },
            timeout=10,
        )
        print(f"Notification sent (status {r.status_code})")
    except Exception as e:
        print(f"Notification failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    log_date  = today_et()
    yesterday = yesterday_et()
    ntfy_topic = os.environ.get("NTFY_TOPIC", "")

    print(f"=== Weather Fetch: {log_date} ===\n")

    forecasts, actuals = load_data()
    results = {"fetched": {}, "errors": [], "actual": None}

    # ── 1. Fetch forecasts from all 3 sources ──────────────────────────────
    for src in SOURCES:
        print(f"Fetching {src['label']} ({src['site']})...")
        try:
            days = fetch_forecast(src, log_date)
            print(f"  ✓ {len(days)} days retrieved")

            # Replace any existing entries for this source+date
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
            print(f"  ✗ Error: {e}")
            results["errors"].append(f"{src['label']}: {e}")

    # ── 2. Fetch yesterday's actuals ───────────────────────────────────────
    print(f"\nFetching actuals for {yesterday}...")
    try:
        act = fetch_actual(yesterday)
        actuals[yesterday] = {
            "high":          act.get("high"),
            "low":           act.get("low"),
            "cloud_cover":   act.get("cloud_cover"),
            "precip_amount": act.get("precip_amount"),
            "condition":     act.get("condition", ""),
        }
        print(f"  ✓ H:{act.get('high')}° / L:{act.get('low')}° — {act.get('condition','')}")
        results["actual"] = act

    except Exception as e:
        print(f"  ✗ Error: {e}")
        results["errors"].append(f"Actuals ({yesterday}): {e}")

    # ── 3. Save ────────────────────────────────────────────────────────────
    save_data(forecasts, actuals)
    print(f"\nSaved: {len(forecasts)} forecast entries, {len(actuals)} actuals on file")

    # ── 4. Notify ──────────────────────────────────────────────────────────
    src_lines = []
    for src in SOURCES:
        n = results["fetched"].get(src["id"])
        src_lines.append(f"  {src['label']}: {'✓ ' + str(n) + ' days' if n else '✗ failed'}")

    act = results["actual"]
    act_line = (
        f"  Yesterday ({yesterday}): H:{act.get('high')}°/L:{act.get('low')}° {act.get('condition','')}"
        if act else f"  Yesterday ({yesterday}): ✗ not found"
    )

    err_line = f"\n⚠ {len(results['errors'])} error(s):\n" + "\n".join(f"  {e}" for e in results["errors"]) \
               if results["errors"] else ""

    message = (
        f"Forecasts logged for {log_date}:\n"
        + "\n".join(src_lines)
        + f"\n\nActuals:\n{act_line}"
        + err_line
    )

    send_notification(f"⛅ Weather Tracker Updated — {log_date}", message, ntfy_topic)
    print(f"\n{message}")
    print("\n=== Done ===")


if __name__ == "__main__":
    main()
