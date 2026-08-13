import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "v1.3.1"
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sites.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
CURRENT_PATH = DATA_DIR / "current.json"
LAST_STATE_PATH = DATA_DIR / "last_state.json"
HISTORY_PATH = DATA_DIR / "history.csv"
DOCS_CURRENT_PATH = DOCS_DIR / "current.json"

KMA_AUTH_KEY = os.environ.get("KMA_AUTH_KEY", "")
KMA_FORECAST_SERVICE_KEY = os.environ.get("KMA_FORECAST_SERVICE_KEY", "") or os.environ.get("KMA_AUTH_KEY", "")
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://blackturtle-water.github.io/heatstress-monitor/")

AWS_URL = "https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min"
FORECAST_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"


def now_kst():
    return datetime.now(KST)


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path, default):
    try:
        if not path.exists():
            return default
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else default
    except Exception as exc:
        print(f"[WARN] JSON read failed {path}: {exc}", flush=True)
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config():
    cfg = read_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("siteName", "유니드 울산공장")
    cfg.setdefault("address", "울산광역시 남구 상개로 142")
    cfg.setdefault("awsStations", [
        {"id": "898", "name": "장생포"},
        {"id": "954", "name": "온산"},
        {"id": "943", "name": "매곡"},
        {"id": "949", "name": "정자"},
    ])
    cfg.setdefault("forecastGrid", {"nx": "102", "ny": "83", "name": "울산 남구"})
    return cfg


def request_bytes(url, params, timeout=8):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "heatstress-monitor/1.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), full_url, None
    except Exception as exc:
        return b"", full_url, f"HTTP_ERROR: {exc}"


def decode_bytes(raw):
    for enc in ["utf-8", "euc-kr", "cp949"]:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def request_text(url, params, timeout=8):
    raw, full_url, error = request_bytes(url, params, timeout)
    return decode_bytes(raw), full_url, error


def parse_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "-9", "-9.0", "-99", "-99.0", "-99.9", "-999", "-999.0", "-999.9"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return None if value <= -50 else value


def parse_aws(text, station_id):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 18 and parts[0].isdigit() and parts[1] == str(station_id):
            rows.append(parts)
    for row in reversed(rows):
        temp = parse_float(row[8])
        humidity = parse_float(row[14])
        wind = parse_float(row[7]) or parse_float(row[3])
        if temp is not None and humidity is not None:
            return {"observedTime": row[0], "temperature": temp, "humidity": humidity, "windSpeed": wind}
    return None


def fetch_weather(config):
    if not KMA_AUTH_KEY:
        return {"ok": False, "apiStatus": "SECRET_MISSING", "apiMessage": "KMA_AUTH_KEY is missing"}
    attempts = []
    for station in config.get("awsStations", []):
        sid = str(station.get("id", ""))
        name = str(station.get("name", sid))
        for back in [10, 20, 30]:
            tm2 = (now_kst() - timedelta(minutes=back)).strftime("%Y%m%d%H%M")
            text, _, error = request_text(AWS_URL, {"tm2": tm2, "stn": sid, "disp": "0", "help": "1", "authKey": KMA_AUTH_KEY}, timeout=6)
            print(f"[DEBUG] AWS station={name}({sid}) tm2={tm2}", flush=True)
            if error:
                attempts.append(error)
                continue
            parsed = parse_aws(text, sid)
            if parsed:
                parsed.update({"ok": True, "apiStatus": "OK", "apiMessage": "", "stationId": sid, "stationName": name})
                return parsed
            attempts.append(f"NO_DATA {name} {tm2}")
    return {"ok": False, "apiStatus": "NO_DATA", "apiMessage": "; ".join(attempts[-5:])}


def c_to_f(c):
    return c * 9 / 5 + 32


def f_to_c(f):
    return (f - 32) * 5 / 9


def heat_index_c(temp_c, humidity):
    if temp_c is None or humidity is None:
        return None
    tf = c_to_f(temp_c)
    if tf < 80:
        simple = 0.5 * (tf + 61 + (tf - 68) * 1.2 + humidity * 0.094)
        return round(f_to_c((simple + tf) / 2), 1)
    hi = (-42.379 + 2.04901523 * tf + 10.14333127 * humidity - 0.22475541 * tf * humidity
          - 0.00683783 * tf * tf - 0.05481717 * humidity * humidity
          + 0.00122874 * tf * tf * humidity + 0.00085282 * tf * humidity * humidity
          - 0.00000199 * tf * tf * humidity * humidity)
    if humidity < 13 and 80 <= tf <= 112:
        hi -= ((13 - humidity) / 4) * math.sqrt(max(0, (17 - abs(tf - 95)) / 17))
    if humidity > 85 and 80 <= tf <= 87:
        hi += ((humidity - 85) / 10) * ((87 - tf) / 5)
    return round(f_to_c(hi), 1)


def decide_level(value):
    if value is None: return "데이터없음"
    if value >= 38: return "매우위험"
    if value >= 35: return "위험"
    if value >= 33: return "경계"
    if value >= 31: return "주의"
    return "정상"


def latest_base_time(dt):
    hours = [2, 5, 8, 11, 14, 17, 20, 23]
    for hour in reversed(hours):
        if dt.hour > hour or (dt.hour == hour and dt.minute >= 10):
            return dt.strftime("%Y%m%d"), f"{hour:02d}00"
    prev = dt - timedelta(days=1)
    return prev.strftime("%Y%m%d"), "2300"


def fetch_forecast(config):
    if not KMA_FORECAST_SERVICE_KEY:
        return {"ok": False, "forecastStatus": "SECRET_MISSING", "forecastMessage": "KMA_FORECAST_SERVICE_KEY is missing"}
    grid = config.get("forecastGrid", {})
    nx, ny = str(grid.get("nx", "102")), str(grid.get("ny", "83"))
    grid_name = str(grid.get("name", "울산 남구"))
    base_date, base_time = latest_base_time(now_kst())
    params = {
        "pageNo": "1", "numOfRows": "1000", "dataType": "JSON",
        "base_date": base_date, "base_time": base_time, "nx": nx, "ny": ny,
        "authKey": KMA_FORECAST_SERVICE_KEY,
    }
    raw, _, error = request_bytes(FORECAST_URL, params, timeout=15)
    if error:
        return {"ok": False, "forecastStatus": "HTTP_ERROR", "forecastMessage": error, "forecastBaseDate": base_date, "forecastBaseTime": base_time, "forecastNx": nx, "forecastNy": ny}
    text = decode_bytes(raw)
    try:
        data = json.loads(text)
    except Exception:
        return {"ok": False, "forecastStatus": "PARSE_ERROR", "forecastMessage": text[:200], "forecastBaseDate": base_date, "forecastBaseTime": base_time, "forecastNx": nx, "forecastNy": ny}
    response = data.get("response", {})
    header = response.get("header", {})
    if str(header.get("resultCode", "")) != "00":
        return {"ok": False, "forecastStatus": "API_ERROR", "forecastMessage": str(header.get("resultMsg", header.get("resultCode", "UNKNOWN"))), "forecastBaseDate": base_date, "forecastBaseTime": base_time, "forecastNx": nx, "forecastNy": ny}
    items = response.get("body", {}).get("items", {}).get("item", [])
    today = now_kst().strftime("%Y%m%d")
    by_time = {}
    for item in items:
        if str(item.get("fcstDate")) != today:
            continue
        key = str(item.get("fcstTime", ""))
        category = str(item.get("category", ""))
        if category in {"TMP", "REH"}:
            by_time.setdefault(key, {})[category] = parse_float(item.get("fcstValue"))
    points = []
    for time_text, values in sorted(by_time.items()):
        temp, humidity = values.get("TMP"), values.get("REH")
        if temp is None or humidity is None:
            continue
        apparent = heat_index_c(temp, humidity)
        points.append({"time": time_text, "temperature": temp, "humidity": humidity, "apparentTemperature": apparent, "level": decide_level(apparent)})
    if not points:
        return {"ok": False, "forecastStatus": "NO_VALID_FORECAST", "forecastMessage": "오늘 TMP/REH 예보값이 없습니다.", "forecastBaseDate": base_date, "forecastBaseTime": base_time, "forecastNx": nx, "forecastNy": ny}
    peak = max(points, key=lambda x: x["apparentTemperature"])
    return {
        "ok": True, "forecastStatus": "OK", "forecastMessage": "", "forecastItems": points,
        "forecastMaxApparentTemperature": peak["apparentTemperature"], "forecastMaxTime": peak["time"],
        "forecastMaxLevel": peak["level"], "forecastMaxTemperature": peak["temperature"],
        "forecastMaxHumidity": peak["humidity"], "forecastBaseDate": base_date,
        "forecastBaseTime": base_time, "forecastNx": nx, "forecastNy": ny, "forecastGridName": grid_name,
    }


def regular_key(dt):
    return f"{dt.strftime('%Y-%m-%d')}_{dt.hour:02d}"


def determine_notification(last_state, level, dt):
    previous = last_state.get("level", "정상")
    if 8 <= dt.hour <= 17 and previous != level and level != "데이터없음":
        return True, "level_change"
    reports = last_state.get("regularReports", {})
    if dt.hour in [8, 13] and regular_key(dt) not in reports and level != "데이터없음":
        return True, "regular_08" if dt.hour == 8 else "regular_13"
    return False, "none"


def actions_for(level):
    return {
        "정상": ["수분 공급과 휴식 관리를 유지해 주세요."],
        "주의": ["시원하고 깨끗한 물을 충분히 제공해 주세요.", "폭염작업 시 적절한 휴식을 부여해 주세요.", "작업자 건강상태를 주기적으로 확인해 주세요."],
        "경계": ["매 2시간 이내 20분 이상 휴식을 부여해 주세요.", "작업시간 조정 또는 옥외작업 단축을 검토해 주세요.", "민감군 작업자를 추가 확인해 주세요."],
        "위험": ["무더위 시간대 옥외작업 중지를 검토해 주세요.", "작업시간 조정 또는 단축을 시행해 주세요.", "작업자 상태를 수시로 확인해 주세요."],
        "매우위험": ["긴급조치 외 옥외작업 중지를 검토해 주세요.", "온열질환 의심자를 즉시 시원한 장소로 이동시켜 주세요.", "의식이 없으면 즉시 119에 신고해 주세요."],
    }.get(level, ["대시보드 상태를 확인해 주세요."])


def send_teams(current, reason):
    if not TEAMS_WEBHOOK_URL:
        return False
    reason_text = {"regular_08": "08:00 정기보고", "regular_13": "13:00 정기보고", "level_change": "단계변경"}.get(reason, "알림")
    forecast_text = "예보 미제공"
    if current.get("forecastMaxApparentTemperature") is not None:
        forecast_text = f"오늘 예상 최고 {current['forecastMaxApparentTemperature']:.1f}℃ / {current.get('forecastMaxLevel')} / {current.get('forecastMaxTime')}"
    lines = [
        f"{reason_text} | {current['level']} | 체감온도 {current['apparentTemperature']:.1f}℃", "",
        f"현재 단계: {current['level']}", f"현재 체감온도: {current['apparentTemperature']:.1f}℃",
        f"기온: {current['temperature']:.1f}℃ / 습도: {current['humidity']:.1f}% / 풍속: {current.get('windSpeed') or '-'} m/s",
        forecast_text, "", "필요 조치",
    ] + [f"- {x}" for x in actions_for(current["level"])]
    payload = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": lines[0], "weight": "Bolder", "size": "Large", "wrap": True}, {"type": "TextBlock", "text": "\n".join(lines[1:]), "wrap": True}], "actions": [{"type": "Action.OpenUrl", "title": "대시보드 바로가기", "url": DASHBOARD_URL}]}}]}
    try:
        req = urllib.request.Request(TEAMS_WEBHOOK_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[WARN] Teams failed: {exc}", flush=True)
        return False


def append_history(current):
    fields = ["observedAt", "siteName", "address", "apparentTemperature", "temperature", "humidity", "windSpeed", "level", "previousLevel", "levelChanged", "notificationReason", "teamsNotified", "apiStatus", "apiMessage", "awsStation", "awsStationName", "awsObservedTime"]
    exists = HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists: writer.writeheader()
        writer.writerow({k: current.get(k, "") for k in fields})


def main():
    print(f"=== Heat Stress Monitor {VERSION} ===", flush=True)
    ensure_dirs()
    config = load_config()
    dt = now_kst()
    last = read_json(LAST_STATE_PATH, {"level": "정상", "regularReports": {}})
    weather = fetch_weather(config)
    forecast = fetch_forecast(config)
    if weather.get("ok"):
        apparent = heat_index_c(weather["temperature"], weather["humidity"])
        level = decide_level(apparent)
        current = {
            "observedAt": dt.strftime("%Y-%m-%d %H:%M:%S"), "siteName": config["siteName"], "address": config["address"],
            "awsStation": weather["stationId"], "awsStationName": weather["stationName"], "awsObservedTime": weather["observedTime"],
            "apparentTemperature": apparent, "temperature": weather["temperature"], "humidity": weather["humidity"], "windSpeed": weather.get("windSpeed"),
            "level": level, "previousLevel": last.get("level", "정상"), "apiStatus": "OK", "apiMessage": "",
        }
    else:
        cached = read_json(CURRENT_PATH, {})
        current = dict(cached) if cached.get("apparentTemperature") is not None else {"apparentTemperature": None, "temperature": None, "humidity": None, "windSpeed": None, "level": "데이터없음"}
        current.update({"observedAt": dt.strftime("%Y-%m-%d %H:%M:%S"), "siteName": config["siteName"], "address": config["address"], "previousLevel": last.get("level", "정상"), "apiStatus": "STALE_DATA", "apiMessage": weather.get("apiMessage", "")})
    current.update({k: v for k, v in forecast.items() if k != "ok"})
    notify, reason = determine_notification(last, current["level"], dt)
    current["levelChanged"] = reason == "level_change"
    current["notificationReason"] = reason
    current["teamsNotified"] = send_teams(current, reason) if notify else False
    write_json(CURRENT_PATH, current)
    write_json(DOCS_CURRENT_PATH, current)
    append_history(current)
    reports = last.get("regularReports", {}) if isinstance(last.get("regularReports"), dict) else {}
    if reason in ["regular_08", "regular_13"] and current["teamsNotified"]:
        reports[regular_key(dt)] = current["observedAt"]
    write_json(LAST_STATE_PATH, {"level": current["level"], "apparentTemperature": current.get("apparentTemperature"), "observedAt": current["observedAt"], "temperature": current.get("temperature"), "humidity": current.get("humidity"), "windSpeed": current.get("windSpeed"), "awsStation": current.get("awsStation"), "awsStationName": current.get("awsStationName"), "awsObservedTime": current.get("awsObservedTime"), "regularReports": reports})
    print(json.dumps(current, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
