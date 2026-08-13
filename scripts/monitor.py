import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "v1.0.1"
KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sites.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
CURRENT_PATH = DATA_DIR / "current.json"
LAST_STATE_PATH = DATA_DIR / "last_state.json"
HISTORY_PATH = DATA_DIR / "history.csv"
DASHBOARD_CURRENT_PATH = DOCS_DIR / "current.json"

KMA_AUTH_KEY = os.environ.get("KMA_AUTH_KEY")
KMA_FORECAST_SERVICE_KEY = os.environ.get("KMA_FORECAST_SERVICE_KEY") or KMA_AUTH_KEY
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://blackturtle-water.github.io/heatstress-monitor/")
AWS_URL = "https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min"
FORECAST_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"


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
        if not text:
            return default
        return json.loads(text)
    except Exception as exc:
        print(f"[WARN] Failed to read JSON {path}: {exc}", flush=True)
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    config = read_json(CONFIG_PATH, None)
    if not isinstance(config, dict):
        raise RuntimeError("config/sites.json is missing or invalid")
    if "siteName" not in config or "address" not in config:
        raise RuntimeError("config/sites.json must include siteName and address")
    if "awsStations" not in config:
        if "awsStation" in config:
            config["awsStations"] = [{"id": str(config["awsStation"]), "name": str(config["awsStation"])}]
        else:
            raise RuntimeError("config/sites.json must include awsStations")
    return config


def get_station_list(config):
    stations = []
    for item in config.get("awsStations", []):
        if isinstance(item, dict):
            sid = str(item.get("id", "")).strip()
            name = str(item.get("name", sid)).strip()
        else:
            sid = str(item).strip()
            name = sid
        if sid:
            stations.append({"id": sid, "name": name or sid})
    if not stations:
        raise RuntimeError("No AWS stations configured")
    return stations


def request_text(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": "heatstress-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        for enc in ["utf-8", "euc-kr", "cp949"]:
            try:
                return raw.decode(enc), full_url, None
            except UnicodeDecodeError:
                pass
        return raw.decode("utf-8", errors="replace"), full_url, None
    except Exception as exc:
        return "", full_url, f"HTTP_ERROR: {exc}"


def parse_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "-9", "-9.0", "-99", "-99.0", "-99.9", "-999", "-999.0", "-999.9", "-9999", "-9999.0", "-9999.9"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number <= -50:
        return None
    return number


def parse_aws_response(text, station_id):
    if not text or not text.strip():
        return None, "EMPTY_RESPONSE"
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 18:
            continue
        if not parts[0].isdigit():
            continue
        if parts[1] != str(station_id):
            continue
        rows.append(parts)
    if not rows:
        return None, "NO_DATA_ROWS"
    for row in reversed(rows):
        wind_speed = parse_float(row[7]) or parse_float(row[3])
        temperature = parse_float(row[8])
        humidity = parse_float(row[14])
        if temperature is None or humidity is None:
            continue
        return {
            "observedTime": row[0],
            "temperature": temperature,
            "humidity": humidity,
            "windSpeed": wind_speed,
            "rawRow": row,
        }, None
    return None, "NO_VALID_OBSERVATION_VALUES"


def fetch_weather(config):
    if not KMA_AUTH_KEY:
        return {"ok": False, "apiStatus": "SECRET_MISSING", "apiMessage": "KMA_AUTH_KEY secret is missing", "attempts": []}
    stations = get_station_list(config)
    dt = now_kst()
    minutes_list = [10, 20, 30, 60]
    attempts = []
    for station in stations:
        sid = station["id"]
        name = station["name"]
        for minutes_back in minutes_list:
            tm2 = (dt - timedelta(minutes=minutes_back)).strftime("%Y%m%d%H%M")
            params = {"tm2": tm2, "stn": sid, "disp": "0", "help": "1", "authKey": KMA_AUTH_KEY}
            text, full_url, error = request_text(AWS_URL, params)
            print(f"[DEBUG] AWS station={name}({sid}) tm2={tm2}", flush=True)
            if error:
                attempts.append({"stationId": sid, "stationName": name, "tm2": tm2, "status": "HTTP_ERROR", "message": error})
                print(f"[WARN] {error}", flush=True)
                continue
            parsed, parse_error = parse_aws_response(text, sid)
            if parsed:
                return {
                    "ok": True,
                    "apiStatus": "OK",
                    "apiMessage": "",
                    "stationId": sid,
                    "stationName": name,
                    "observedTime": parsed["observedTime"],
                    "temperature": parsed["temperature"],
                    "humidity": parsed["humidity"],
                    "windSpeed": parsed["windSpeed"],
                    "url": full_url,
                }
            attempts.append({"stationId": sid, "stationName": name, "tm2": tm2, "status": "NO_VALID_DATA", "message": parse_error})
            print(f"[WARN] Parse failed: {parse_error}", flush=True)
    return {"ok": False, "apiStatus": "NO_DATA", "apiMessage": "모든 AWS 후보 관측소에서 유효한 기온/습도 값을 찾지 못했습니다.", "attempts": attempts[:30]}



def get_forecast_grid(config):
    grid = config.get("forecastGrid")
    if isinstance(grid, dict):
        nx = str(grid.get("nx", "")).strip()
        ny = str(grid.get("ny", "")).strip()
        if nx and ny:
            return nx, ny

    nx = str(config.get("forecastNx", "")).strip()
    ny = str(config.get("forecastNy", "")).strip()
    if nx and ny:
        return nx, ny

    # Default candidate for Ulsan Nam-gu area. Override in config/sites.json if needed.
    return "102", "83"


def latest_forecast_base(dt):
    # KMA village forecast base times are released at 02, 05, 08, 11, 14, 17, 20, 23 KST.
    # Use a 10 minute delay after the base time to avoid empty responses immediately after release.
    base_hours = [2, 5, 8, 11, 14, 17, 20, 23]
    for hour in sorted(base_hours, reverse=True):
        if dt.hour > hour or (dt.hour == hour and dt.minute >= 10):
            return dt.strftime("%Y%m%d"), f"{hour:02d}00"

    prev = dt - timedelta(days=1)
    return prev.strftime("%Y%m%d"), "2300"


def request_json(url, params):
    text, full_url, error = request_text(url, params)
    if error:
        return None, full_url, error
    try:
        return json.loads(text), full_url, None
    except Exception as exc:
        return None, full_url, f"JSON_ERROR: {exc}"


def request_json_with_retry(url, params, retry_count=2):
    last_data = None
    last_url = None
    last_error = None
    for attempt in range(retry_count + 1):
        data, full_url, error = request_json(url, params)
        last_data = data
        last_url = full_url
        last_error = error
        if not error:
            return data, full_url, None
        print(f"[WARN] Forecast API attempt {attempt + 1} failed: {error}", flush=True)
    return last_data, last_url, last_error


def fetch_today_forecast(config):
    if not KMA_FORECAST_SERVICE_KEY:
        return {
            "ok": False,
            "forecastStatus": "SECRET_MISSING",
            "forecastMessage": "KMA_FORECAST_SERVICE_KEY or KMA_AUTH_KEY secret is missing.",
            "forecastItems": []
        }

    nx, ny = get_forecast_grid(config)
    dt = now_kst()
    base_date, base_time = latest_forecast_base(dt)

    params = {
        "serviceKey": KMA_FORECAST_SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny
    }

    data, full_url, error = request_json_with_retry(FORECAST_URL, params, retry_count=2)
    if error:
        return {
            "ok": False,
            "forecastStatus": "HTTP_OR_JSON_ERROR",
            "forecastMessage": error,
            "forecastItems": [],
            "forecastNx": nx,
            "forecastNy": ny,
            "forecastBaseDate": base_date,
            "forecastBaseTime": base_time
        }

    try:
        header = data.get("response", {}).get("header", {})
        result_code = str(header.get("resultCode", ""))
        result_msg = str(header.get("resultMsg", ""))
        if result_code and result_code != "00":
            return {
                "ok": False,
                "forecastStatus": "API_ERROR",
                "forecastMessage": result_msg or result_code,
                "forecastItems": [],
                "forecastNx": nx,
                "forecastNy": ny,
                "forecastBaseDate": base_date,
                "forecastBaseTime": base_time
            }

        items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
    except Exception as exc:
        return {
            "ok": False,
            "forecastStatus": "PARSE_ERROR",
            "forecastMessage": str(exc),
            "forecastItems": [],
            "forecastNx": nx,
            "forecastNy": ny,
            "forecastBaseDate": base_date,
            "forecastBaseTime": base_time
        }

    today = dt.strftime("%Y%m%d")
    now_hhmm = dt.strftime("%H%M")
    by_time = {}

    for item in items:
        if str(item.get("fcstDate")) != today:
            continue
        fcst_time = str(item.get("fcstTime", ""))
        if fcst_time < now_hhmm:
            continue
        category = str(item.get("category", ""))
        value = item.get("fcstValue")
        if category not in ["TMP", "REH"]:
            continue
        by_time.setdefault(fcst_time, {})[category] = parse_float(value)

    forecast_points = []
    for fcst_time, values in sorted(by_time.items()):
        temp = values.get("TMP")
        reh = values.get("REH")
        if temp is None or reh is None:
            continue
        apparent = calculate_heat_index_celsius(temp, reh)
        if apparent is None:
            continue
        forecast_points.append({
            "time": fcst_time,
            "temperature": temp,
            "humidity": reh,
            "apparentTemperature": apparent,
            "level": decide_level(apparent)
        })

    if not forecast_points:
        return {
            "ok": False,
            "forecastStatus": "NO_VALID_FORECAST",
            "forecastMessage": "오늘 남은 시간의 TMP/REH 예보값을 찾지 못했습니다.",
            "forecastItems": [],
            "forecastNx": nx,
            "forecastNy": ny,
            "forecastBaseDate": base_date,
            "forecastBaseTime": base_time
        }

    max_point = max(forecast_points, key=lambda x: x["apparentTemperature"])
    return {
        "ok": True,
        "forecastStatus": "OK",
        "forecastMessage": "",
        "forecastItems": forecast_points,
        "forecastMaxApparentTemperature": max_point["apparentTemperature"],
        "forecastMaxTime": max_point["time"],
        "forecastMaxLevel": max_point["level"],
        "forecastMaxTemperature": max_point["temperature"],
        "forecastMaxHumidity": max_point["humidity"],
        "forecastNx": nx,
        "forecastNy": ny,
        "forecastBaseDate": base_date,
        "forecastBaseTime": base_time
    }

def c_to_f(value):
    return value * 9 / 5 + 32


def f_to_c(value):
    return (value - 32) * 5 / 9


def calculate_heat_index_celsius(temperature_c, humidity):
    if temperature_c is None or humidity is None:
        return None
    tf = c_to_f(temperature_c)
    if tf < 80:
        simple = 0.5 * (tf + 61.0 + ((tf - 68.0) * 1.2) + (humidity * 0.094))
        hi_f = (simple + tf) / 2
        return round(f_to_c(hi_f), 1)
    hi_f = (-42.379 + 2.04901523 * tf + 10.14333127 * humidity - 0.22475541 * tf * humidity - 0.00683783 * tf * tf - 0.05481717 * humidity * humidity + 0.00122874 * tf * tf * humidity + 0.00085282 * tf * humidity * humidity - 0.00000199 * tf * tf * humidity * humidity)
    if humidity < 13 and 80 <= tf <= 112:
        hi_f -= ((13 - humidity) / 4) * math.sqrt((17 - abs(tf - 95)) / 17)
    if humidity > 85 and 80 <= tf <= 87:
        hi_f += ((humidity - 85) / 10) * ((87 - tf) / 5)
    return round(f_to_c(hi_f), 1)


def decide_level(apparent_temperature):
    if apparent_temperature is None:
        return "데이터없음"
    if apparent_temperature >= 38:
        return "매우위험"
    if apparent_temperature >= 35:
        return "위험"
    if apparent_temperature >= 33:
        return "경계"
    if apparent_temperature >= 31:
        return "주의"
    return "정상"


def level_rank(level):
    return {"데이터없음": -1, "정상": 0, "주의": 1, "경계": 2, "위험": 3, "매우위험": 4}.get(level, -1)


def load_last_state():
    default = {"level": "정상", "apparentTemperature": None, "observedAt": None, "temperature": None, "humidity": None, "windSpeed": None, "awsStation": None, "awsStationName": None, "awsObservedTime": None, "regularReports": {}}
    state = read_json(LAST_STATE_PATH, default)
    if not isinstance(state, dict):
        return default
    if "regularReports" not in state or not isinstance(state["regularReports"], dict):
        state["regularReports"] = {}
    if "level" not in state:
        state["level"] = "정상"
    return state


def load_current_state():
    state = read_json(CURRENT_PATH, {})
    return state if isinstance(state, dict) else {}


def is_valid_state(state):
    if not isinstance(state, dict):
        return False
    if state.get("level") in [None, "", "데이터없음"]:
        return False
    if state.get("apparentTemperature") is None:
        return False
    if state.get("temperature") is None:
        return False
    if state.get("humidity") is None:
        return False
    return True


def get_last_valid_state(last_state):
    if is_valid_state(last_state):
        return last_state
    current_state = load_current_state()
    if is_valid_state(current_state):
        return current_state
    return None


def is_regular_time(dt):
    return dt.hour in [8, 13]


def regular_key(dt):
    return f"{dt.strftime('%Y-%m-%d')}_{dt.hour:02d}"


def is_alert_time(dt):
    # Data is collected 24 hours a day, but Teams notifications are sent only during work monitoring hours.
    # KST 08:00 through 17:59 are considered notification hours.
    return 8 <= dt.hour <= 17


def should_send_regular(last_state, dt, current_level):
    if current_level == "데이터없음":
        return False
    if not is_regular_time(dt):
        return False
    reports = last_state.get("regularReports", {})
    if not isinstance(reports, dict):
        return True
    return regular_key(dt) not in reports


def should_send_level_change(previous_level, current_level):
    if current_level == "데이터없음":
        return False
    return previous_level != current_level


def determine_notification(last_state, current_level, dt):
    previous_level = last_state.get("level", "정상")
    if should_send_level_change(previous_level, current_level):
        return True, "level_change"
    if should_send_regular(last_state, dt, current_level):
        if dt.hour == 8:
            return True, "regular_08"
        if dt.hour == 13:
            return True, "regular_13"
    return False, "none"


def title_and_actions(level, reason):
    if reason == "regular_08":
        return "📋 [온열질환 정기보고] 08:00 현황", ["금일 온열질환 모니터링을 시작합니다.", "현재 체감온도와 작업환경을 확인해 주세요."]
    if reason == "regular_13":
        return "📋 [온열질환 정기보고] 13:00 현황", ["오후 작업 전 체감온도와 작업환경을 확인해 주세요.", "폭염 단계가 상승하면 추가 알림이 발송됩니다."]
    if level == "정상":
        return "✅ [온열질환 정상복귀] 체감온도 31℃ 미만", ["현재 체감온도는 31℃ 미만입니다.", "다만 작업 전 수분공급과 휴식 관리는 계속 유지해 주세요."]
    if level == "주의":
        return "🟡 [온열질환 주의] 체감온도 31℃ 이상", ["시원하고 깨끗한 물을 충분히 제공해 주세요.", "폭염작업 시 적절한 휴식을 부여해 주세요.", "작업자 건강상태를 주기적으로 확인해 주세요."]
    if level == "경계":
        return "🟠 [온열질환 경계] 체감온도 33℃ 이상", ["폭염작업 시 매 2시간 이내 20분 이상 휴식을 부여해 주세요.", "작업시간대 조정 또는 옥외작업 단축을 검토해 주세요.", "온열질환 민감군 작업자를 추가 확인해 주세요."]
    if level == "위험":
        return "🔴 [온열질환 위험] 체감온도 35℃ 이상", ["무더위 시간대 옥외작업 중지를 검토해 주세요.", "작업시간 조정 또는 옥외작업 단축을 시행해 주세요.", "작업자 건강상태를 수시로 확인해 주세요."]
    if level == "매우위험":
        return "🚨 [온열질환 매우위험] 체감온도 38℃ 이상", ["긴급조치 작업 외 옥외작업 중지를 검토해 주세요.", "작업자 상태를 수시로 확인해 주세요.", "온열질환 의심자는 즉시 시원한 장소로 이동시켜 주세요.", "의식이 없는 경우 즉시 119에 신고해 주세요."]
    return "⚪ [온열질환 데이터 없음]", ["대시보드 상태를 확인해 주세요."]


def direction_text(previous_level, current_level, reason):
    if reason in ["regular_08", "regular_13"]:
        return "정기보고"
    if level_rank(current_level) > level_rank(previous_level):
        return "단계상승"
    if level_rank(current_level) < level_rank(previous_level):
        return "단계하향"
    return "단계변경"


def build_message(config, current, previous_level, reason):
    level = current["level"]
    base_title, actions = title_and_actions(level, reason)
    direction = direction_text(previous_level, level, reason)

    temp_text = "-" if current.get("apparentTemperature") is None else f"{current['apparentTemperature']:.1f}℃"
    air_text = "-" if current.get("temperature") is None else f"{current['temperature']:.1f}℃"
    hum_text = "-" if current.get("humidity") is None else f"{current['humidity']:.1f}%"
    wind_text = "-" if current.get("windSpeed") is None else f"{current['windSpeed']:.1f} m/s"
    station_text = current.get("awsStationName") or current.get("awsStation") or "-"

    if reason == "regular_08":
        prefix = "📋 08:00 정기보고"
    elif reason == "regular_13":
        prefix = "📋 13:00 정기보고"
    elif reason == "level_change":
        prefix = "🚨 온열질환 단계변경"
    else:
        prefix = "📌 온열질환 알림"

    title = f"{prefix} | {level} | 체감온도 {temp_text}"

    if level == "정상":
        safety_title = "안내"
    else:
        safety_title = "필요 조치"

    lines = [
        f"{level} / 체감온도 {temp_text}",
        "",
        "핵심 현황",
        f"- 현재 단계: {level}",
        f"- 체감온도: {temp_text}",
        f"- 기온: {air_text}",
        f"- 습도: {hum_text}",
        f"- 풍속: {wind_text}",
        "",
        f"변화: {direction} ({previous_level} → {level})",
        f"관측: {station_text}",
        f"조회: {current['observedAt']}",
        "",
        safety_title,
    ]

    for item in actions:
        lines.append(f"- {item}")

    lines.extend(["", "대시보드", DASHBOARD_URL])
    return title, "\n".join(lines)


def send_teams(title, text):
    if not TEAMS_WEBHOOK_URL:
        print("TEAMS_WEBHOOK_URL secret is missing. Skip Teams notification.", flush=True)
        return False
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Large", "wrap": True},
                        {"type": "TextBlock", "text": text, "wrap": True},
                    ],
                    "actions": [{"type": "Action.OpenUrl", "title": "대시보드 바로가기", "url": DASHBOARD_URL}],
                },
            }
        ],
    }
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(TEAMS_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        print(f"Teams response status: {status}", flush=True)
        print(f"Teams response body: {body[:300]}", flush=True)
        return 200 <= status < 300
    except Exception as exc:
        print(f"[WARN] Teams notification failed: {exc}", flush=True)
        return False


def append_history(row):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = HISTORY_PATH.exists()
    fields = ["observedAt", "siteName", "address", "apparentTemperature", "temperature", "humidity", "windSpeed", "level", "previousLevel", "levelChanged", "notificationReason", "teamsNotified", "apiStatus", "apiMessage", "awsStation", "awsStationName", "awsObservedTime"]
    with HISTORY_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def save_current(current):
    write_json(CURRENT_PATH, current)
    write_json(DASHBOARD_CURRENT_PATH, current)


def make_stale_current(config, last_valid, observed_at, previous_level):
    return {
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "awsStation": last_valid.get("awsStation"),
        "awsStationName": last_valid.get("awsStationName"),
        "awsObservedTime": last_valid.get("awsObservedTime") or last_valid.get("observedAt"),
        "apparentTemperature": last_valid.get("apparentTemperature"),
        "temperature": last_valid.get("temperature"),
        "humidity": last_valid.get("humidity"),
        "windSpeed": last_valid.get("windSpeed"),
        "level": last_valid.get("level", "정상"),
        "previousLevel": previous_level,
        "levelChanged": False,
        "notificationReason": "stale_data",
        "teamsNotified": False,
        "apiStatus": "STALE_DATA",
        "apiMessage": "AWS 관측자료를 일시적으로 가져오지 못해 마지막 정상 데이터를 표시 중입니다.",
    }


def make_unavailable_current(config, weather, observed_at, previous_level):
    return {
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "awsStation": None,
        "awsStationName": None,
        "awsObservedTime": None,
        "apparentTemperature": None,
        "temperature": None,
        "humidity": None,
        "windSpeed": None,
        "level": "데이터없음",
        "previousLevel": previous_level,
        "levelChanged": False,
        "notificationReason": "data_unavailable",
        "teamsNotified": False,
        "apiStatus": weather.get("apiStatus", "NO_DATA"),
        "apiMessage": weather.get("apiMessage", "유효한 관측자료를 찾지 못했습니다."),
    }


def main():
    print(f"=== Heat Stress Monitor {VERSION} ===", flush=True)
    ensure_dirs()
    config = load_config()
    last_state = load_last_state()
    dt = now_kst()
    observed_at = dt.strftime("%Y-%m-%d %H:%M:%S")
    previous_level = last_state.get("level", "정상")
    weather = fetch_weather(config)
    forecast = fetch_today_forecast(config)

    if not weather.get("ok"):
        last_valid = get_last_valid_state(last_state)
        if last_valid:
            current = make_stale_current(config, last_valid, observed_at, previous_level)
            print("AWS data unavailable. Using last valid state.", flush=True)
        else:
            current = make_unavailable_current(config, weather, observed_at, previous_level)
            print("AWS data unavailable. No valid cached state found.", flush=True)
        current.update({
            "forecastStatus": forecast.get("forecastStatus"),
            "forecastMessage": forecast.get("forecastMessage"),
            "forecastMaxApparentTemperature": forecast.get("forecastMaxApparentTemperature"),
            "forecastMaxTime": forecast.get("forecastMaxTime"),
            "forecastMaxLevel": forecast.get("forecastMaxLevel"),
            "forecastBaseDate": forecast.get("forecastBaseDate"),
            "forecastBaseTime": forecast.get("forecastBaseTime"),
            "forecastNx": forecast.get("forecastNx"),
            "forecastNy": forecast.get("forecastNy")
        })
        print(json.dumps({"apiStatus": weather.get("apiStatus"), "apiMessage": weather.get("apiMessage"), "attemptsSample": weather.get("attempts", [])[:20], "forecastStatus": forecast.get("forecastStatus"), "forecastMessage": forecast.get("forecastMessage")}, ensure_ascii=False, indent=2), flush=True)
        save_current(current)
        append_history({
            "observedAt": observed_at,
            "siteName": config["siteName"],
            "address": config["address"],
            "apparentTemperature": "" if current.get("apparentTemperature") is None else current.get("apparentTemperature"),
            "temperature": "" if current.get("temperature") is None else current.get("temperature"),
            "humidity": "" if current.get("humidity") is None else current.get("humidity"),
            "windSpeed": "" if current.get("windSpeed") is None else current.get("windSpeed"),
            "level": current.get("level"),
            "previousLevel": previous_level,
            "levelChanged": "N",
            "notificationReason": current.get("notificationReason"),
            "teamsNotified": "N",
            "apiStatus": current.get("apiStatus"),
            "apiMessage": current.get("apiMessage"),
            "awsStation": current.get("awsStation") or "",
            "awsStationName": current.get("awsStationName") or "",
            "awsObservedTime": current.get("awsObservedTime") or "",
        })
        print("Current status:", flush=True)
        print(json.dumps(current, ensure_ascii=False, indent=2), flush=True)
        return

    temperature = weather["temperature"]
    humidity = weather["humidity"]
    wind_speed = weather.get("windSpeed")
    apparent_temperature = calculate_heat_index_celsius(temperature, humidity)
    current_level = decide_level(apparent_temperature)
    notify, reason = determine_notification(last_state, current_level, dt)
    current = {
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "awsStation": weather.get("stationId"),
        "awsStationName": weather.get("stationName"),
        "awsObservedTime": weather.get("observedTime"),
        "apparentTemperature": apparent_temperature,
        "temperature": temperature,
        "humidity": humidity,
        "windSpeed": wind_speed,
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": reason == "level_change",
        "notificationReason": reason,
        "teamsNotified": False,
        "apiStatus": "OK",
        "apiMessage": "",
        "forecastStatus": forecast.get("forecastStatus"),
        "forecastMessage": forecast.get("forecastMessage"),
        "forecastMaxApparentTemperature": forecast.get("forecastMaxApparentTemperature"),
        "forecastMaxTime": forecast.get("forecastMaxTime"),
        "forecastMaxLevel": forecast.get("forecastMaxLevel"),
        "forecastBaseDate": forecast.get("forecastBaseDate"),
        "forecastBaseTime": forecast.get("forecastBaseTime"),
        "forecastNx": forecast.get("forecastNx"),
        "forecastNy": forecast.get("forecastNy"),
    }
    if notify:
        title, message = build_message(config, current, previous_level, reason)
        current["teamsNotified"] = send_teams(title, message)
    else:
        print(f"No notification. Previous={previous_level}, Current={current_level}", flush=True)
    save_current(current)
    reports = last_state.get("regularReports", {})
    if not isinstance(reports, dict):
        reports = {}
    if reason in ["regular_08", "regular_13"] and current["teamsNotified"]:
        reports[regular_key(dt)] = observed_at
    write_json(LAST_STATE_PATH, {
        "level": current_level,
        "apparentTemperature": apparent_temperature,
        "observedAt": observed_at,
        "temperature": temperature,
        "humidity": humidity,
        "windSpeed": wind_speed,
        "awsStation": weather.get("stationId"),
        "awsStationName": weather.get("stationName"),
        "awsObservedTime": weather.get("observedTime"),
        "regularReports": reports,
    })
    append_history({
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "apparentTemperature": apparent_temperature,
        "temperature": temperature,
        "humidity": humidity,
        "windSpeed": "" if wind_speed is None else wind_speed,
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": "Y" if reason == "level_change" else "N",
        "notificationReason": reason,
        "teamsNotified": "Y" if current["teamsNotified"] else "N",
        "apiStatus": "OK",
        "apiMessage": "",
        "awsStation": weather.get("stationId") or "",
        "awsStationName": weather.get("stationName") or "",
        "awsObservedTime": weather.get("observedTime") or "",
    })
    print("Current status:", flush=True)
    print(json.dumps(current, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        try:
            ensure_dirs()
            error_current = {
                "observedAt": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                "siteName": "유니드 울산공장",
                "address": "울산광역시 남구 상개로 142",
                "awsStation": None,
                "awsStationName": None,
                "awsObservedTime": None,
                "apparentTemperature": None,
                "temperature": None,
                "humidity": None,
                "windSpeed": None,
                "level": "데이터없음",
                "previousLevel": "알수없음",
                "levelChanged": False,
                "notificationReason": "script_error",
                "teamsNotified": False,
                "apiStatus": "SCRIPT_ERROR",
                "apiMessage": str(exc),
            }
            save_current(error_current)
            append_history({
                "observedAt": error_current["observedAt"],
                "siteName": error_current["siteName"],
                "address": error_current["address"],
                "apparentTemperature": "",
                "temperature": "",
                "humidity": "",
                "windSpeed": "",
                "level": "데이터없음",
                "previousLevel": "알수없음",
                "levelChanged": "N",
                "notificationReason": "script_error",
                "teamsNotified": "N",
                "apiStatus": "SCRIPT_ERROR",
                "apiMessage": str(exc),
                "awsStation": "",
                "awsStationName": "",
                "awsObservedTime": "",
            })
            print("Script error was recorded to dashboard data.", flush=True)
            print(json.dumps(error_current, ensure_ascii=False, indent=2), flush=True)
        except Exception as inner_exc:
            print(f"[ERROR] Failed to write error status: {inner_exc}", file=sys.stderr, flush=True)
        sys.exit(0)
