"""
SolarSync ETL Pipeline
======================
Extract  : Open-Meteo REST API (weather + solar radiation JSON)
Transform: Raw unstructured JSON -> 3NF-normalised analytical schema
Load     : Supabase PostgreSQL via REST API with pgBouncer connection pooling

Schedule : Render cron job (every 1 hour) or system cron
"""
import os, json, logging, requests, random
from datetime import datetime, timezone
from math import sin, pi
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("etl_pipeline.log")],
)
log = logging.getLogger(__name__)

SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_LAT, DEFAULT_LON = 11.0168, 76.9558

BASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# ══════════════════════════════════════════════════════════════
# STEP 1 - EXTRACT: Pull raw JSON from Open-Meteo REST API
# ══════════════════════════════════════════════════════════════
def extract_weather(lat=DEFAULT_LAT, lon=DEFAULT_LON):
    params = {
        "latitude": lat, "longitude": lon,
        "daily": ",".join([
            "temperature_2m_max","temperature_2m_min",
            "precipitation_probability_max","uv_index_max",
            "shortwave_radiation_sum","wind_speed_10m_max"
        ]),
        "hourly": "direct_radiation",
        "timezone": "Asia/Kolkata", "forecast_days": 7,
    }
    log.info(f"[EXTRACT] Calling Open-Meteo API lat={lat} lon={lon}")
    r = requests.get(OPEN_METEO_URL, params=params, timeout=15)
    r.raise_for_status()
    raw = r.json()
    log.info(f"[EXTRACT] Received {len(raw.get('daily',{}).get('time',[]))} daily records")
    return raw

# ══════════════════════════════════════════════════════════════
# STEP 2 - TRANSFORM: JSON -> 3NF analytical schema
# ══════════════════════════════════════════════════════════════
def transform_weather(raw, system_kw=6.0):
    """
    Transforms unstructured Open-Meteo JSON into atomic 3NF schema:
    - Each attribute depends only on forecast_date (primary key)
    - Derived solar metrics isolated from raw weather attributes
    - No transitive dependencies
    """
    d = raw.get("daily", {})
    records = []
    for i, date in enumerate(d.get("time", [])):
        rad  = (d.get("shortwave_radiation_sum") or [])[i] or 0
        rp   = (d.get("precipitation_probability_max") or [])[i] or 0
        uv   = (d.get("uv_index_max") or [])[i] or 0
        # Solar generation formula: kWh = Radiation(MJ/m2) * Area(m2) * Efficiency / 3.6
        pkwh = round(min(rad * 40 * 0.18 * (1-rp/100*0.75) / 3.6, system_kw*5.5), 2)
        icon = ("rainy" if rp>70 else "partly_cloudy_rainy" if rp>40
                else "partly_cloudy" if rp>20 else "cloudy" if uv<4 else "sunny")
        records.append({
            "forecast_date": date,
            "temp_max_c": (d.get("temperature_2m_max") or [])[i],
            "temp_min_c": (d.get("temperature_2m_min") or [])[i],
            "rain_probability_pct": rp,
            "uv_index_max": uv,
            "radiation_mj_m2": rad,
            "wind_speed_kmh": (d.get("wind_speed_10m_max") or [])[i],
            "predicted_kwh": pkwh,
            "weather_icon": icon,
            "latitude": raw.get("latitude"),
            "longitude": raw.get("longitude"),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        })
    log.info(f"[TRANSFORM] {len(records)} weather rows -> 3NF schema")
    return records

def transform_telemetry(user_id, system_kw=6.0):
    """
    Transforms inverter JSON payloads -> structured telemetry schema.
    Production: real Modbus/RS485 inverter data via edge device.
    Current: simulated IoT data demonstrating schema transformation.
    """
    now, rows = datetime.now(timezone.utc), []
    for h in range(7, 19):
        ir  = max(0, sin(pi*(h-6)/13))
        kwh = round(max(0, system_kw*ir*0.87 + random.uniform(-0.15,0.15)), 3)
        rows.append({
            "user_id": user_id,
            "reading_hour": h,
            "reading_date": now.date().isoformat(),
            "kwh_generated": kwh,
            "grid_export_kwh": round(kwh*0.35, 3),
            "consumption_kwh": round(kwh*0.65+random.uniform(0.2,0.8), 3),
            "panel_temp_c": round(28+ir*22+random.uniform(-2,2), 1),
            "irradiance_wm2": round(ir*950+random.uniform(-30,30), 1),
            "recorded_at": now.isoformat(),
        })
    log.info(f"[TRANSFORM] {len(rows)} telemetry rows for user {user_id}")
    return rows

# ══════════════════════════════════════════════════════════════
# STEP 3 - LOAD: Batch upsert into Supabase with pgBouncer
# ══════════════════════════════════════════════════════════════
def load(table, records, merge=False):
    """
    Batched upsert to Supabase REST API.
    pgBouncer connection pooling handled at Supabase infrastructure layer.
    Batch size=100 rows for optimal throughput vs latency tradeoff.
    """
    if not records:
        return 0
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    hdrs = {**BASE_HEADERS,
            "Prefer": "resolution=merge-duplicates,return=representation" if merge
                      else "return=representation"}
    loaded = 0
    for i in range(0, len(records), 100):
        batch = records[i:i+100]
        r = requests.post(url, headers=hdrs, data=json.dumps(batch), timeout=30)
        if r.status_code in (200, 201):
            loaded += len(batch)
            log.info(f"[LOAD] Batch {i//100+1} -> {table} ({len(batch)} rows) OK")
        else:
            log.error(f"[LOAD] FAIL {r.status_code}: {r.text[:200]}")
    return loaded

def update_energy_stats(user_id, telemetry):
    today = sum(r["kwh_generated"] for r in telemetry)
    payload = {
        "user_id": user_id,
        "today": round(today, 2),
        "this_month": round(today*22, 2),
        "total_saved": round(today*22*7.5, 2),
        "co2_avoided": round(today*22*0.82, 2),
        "efficiency": 87,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    hdrs = {**BASE_HEADERS, "Prefer": "resolution=merge-duplicates"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/energy_stats",
                      headers=hdrs, data=json.dumps(payload), timeout=15)
    log.info(f"[LOAD] energy_stats -> {'OK' if r.status_code in (200,201) else 'FAIL'}")

# ══════════════════════════════════════════════════════════════
# PIPELINE ORCHESTRATOR
# ══════════════════════════════════════════════════════════════
def run_pipeline(user_id=None, lat=DEFAULT_LAT, lon=DEFAULT_LON):
    log.info("="*55)
    log.info("SolarSync ETL Pipeline START")
    log.info("="*55)
    summary = {"weather_rows": 0, "telemetry_rows": 0, "errors": []}
    try:
        raw   = extract_weather(lat, lon)
        clean = transform_weather(raw)
        summary["weather_rows"] = load("weather_forecasts", clean, merge=True)
    except Exception as e:
        log.error(f"Weather ETL error: {e}")
        summary["errors"].append(f"weather:{e}")
    if user_id:
        try:
            tele = transform_telemetry(user_id)
            summary["telemetry_rows"] = load("energy_readings", tele)
            update_energy_stats(user_id, tele)
        except Exception as e:
            log.error(f"Telemetry ETL error: {e}")
            summary["errors"].append(f"telemetry:{e}")
    log.info(f"ETL DONE: {summary}")
    return summary

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="SolarSync ETL Pipeline")
    p.add_argument("--user-id", default=None, help="Supabase user UUID")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    a = p.parse_args()
    print(json.dumps(run_pipeline(a.user_id, a.lat, a.lon), indent=2))
