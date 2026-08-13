import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "v1.1.1"
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
FORECAST_URL = "https://apihub.kma.go.kr/openApi/LivingWthrIdxServiceV3/getSenTaIdxV3"


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


def request_text(url, params, timeout_seconds=6):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": "heatstress-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
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
    minutes_list = [10, 20, 30]
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



def get_forecast_areas(config):
    areas = []
    for item in config.get("forecastAreas", []):
        if isinstance(item, dict):
            area_no = str(item.get("areaNo", "")).strip()
            area_name = str(item.get("name", area_no)).strip()
        else:
            area_no = str(item).strip()
            area_name = area_no
        if area_no:
            areas.append({"areaNo": area_no, "name": area_name or area_no})

    if not areas:
        # Default fallback candidates near Ulsan Nam-gu industrial area.
        areas = [
            {"areaNo": "3114064000", "name": "선암동"},
            {"areaNo": "3114067000", "name": "야음장생포동"},
            {"areaNo": "3114062500", "name": "대현동"},
            {"areaNo": "3114063500", "name": "수암동"},
            {"areaNo": "3114057000", "name": "삼산동"}
        ]
    return areas


def get_forecast_request_code(config):
    return str(config.get("forecastRequestCode", "A48")).strip() or "A48"


def forecast_time_candidates(dt):
    # 생활기상지수 API는 발표시각을 요구한다. 현재시각부터 과거 방향으로 넓게 재시도한다.
    candidates = []
    for hours_back in [0, 1, 2, 3, 6, 9, 12, 24]:
        t = dt - timedelta(hours=hours_back)
        candidates.append(t.strftime("%Y%m%d%H"))
    # 일부 APIHub 화면은 0 입력 시 최신값으로 동작하는 경우가 있어 마지막 후보로 둔다.
    candidates.append("0")
    return candidates


def request_json_or_xml(url, params):
    text, full_url, error = request_text(url, params, timeout_seconds=15)
    if error:
        return None, text, full_url, error
    try:
        return json.loads(text), text, full_url, None
    except Exception:
        return None, text, full_url, None


def flatten_json(obj, prefix=""):
    rows = []
    if isinstance(obj, dict):
        rows.append(obj)
        for value in obj.values():
            rows.extend(flatten_json(value, prefix))
    elif isinstance(obj, list):
        for value in obj:
            rows.extend(flatten_json(value, prefix))
    return rows


def parse_xml_simple_items(text):
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
    except Exception:
        return []
    rows = []
    for elem in root.iter():
        children = list(elem)
        if not children:
            continue
        row = {}
        for child in children:
            tag = child.tag.split("}")[-1]
            row[tag] = (child.text or "").strip()
        if row:
            rows.append(row)
    return rows


def parse_living_heat_forecast(data, raw_text, issue_time_text):
    if data is not None:
        rows = flatten_json(data)
    else:
        rows = parse_xml_simple_items(raw_text)

    points = []
    issue_dt = None
    if issue_time_text and issue_time_text != "0" and len(issue_time_text) == 10:
        try:
            issue_dt = datetime.strptime(issue_time_text, "%Y%m%d%H").replace(tzinfo=KST)
        except Exception:
            issue_dt = None

    for row in rows:
        if not isinstance(row, dict):
            continue

        # Case 1: horizon fields such as h0, h3, h6, h9, h12...
        for key, value in row.items():
            key_text = str(key).lower().strip()
            if not (key_text.startswith("h") and key_text[1:].isdigit()):
                continue
            apparent = parse_float(value)
            if apparent is None:
                continue
            hour_offset = int(key_text[1:])
            if issue_dt:
                target = issue_dt + timedelta(hours=hour_offset)
                target_time = target.strftime("%H%M")
                target_date = target.strftime("%Y%m%d")
            else:
                target_time = f"+{hour_offset}h"
                target_date = ""
            points.append({
                "date": target_date,
                "time": target_time,
                "apparentTemperature": apparent,
                "level": decide_level(apparent),
                "sourceKey": key
            })

        # Case 2: item rows with explicit time and value-style fields.
        time_value = None
        for tkey in ["time", "tm", "fcstTime", "dateTime", "tmFc", "tmEf"]:
            if tkey in row and row.get(tkey):
                time_value = str(row.get(tkey)).strip()
                break

        value_key = None
        for vkey in ["value", "idx", "hidx", "heatIndex", "senTa", "senTaIdx", "ta", "TA"]:
            if vkey in row and parse_float(row.get(vkey)) is not None:
                value_key = vkey
                break

        if value_key:
            apparent = parse_float(row.get(value_key))
            if apparent is not None:
                if time_value and len(time_value) >= 10:
                    target_date = time_value[:8]
                    target_time = time_value[8:12] if len(time_value) >= 12 else time_value[8:10] + "00"
                elif time_value:
                    target_date = ""
                    target_time = time_value
                else:
                    target_date = ""
                    target_time = ""
                points.append({
                    "date": target_date,
                    "time": target_time,
                    "apparentTemperature": apparent,
                    "level": decide_level(apparent),
                    "sourceKey": value_key
                })

    # Deduplicate by time and value.
    unique = []
    seen = set()
    for p in points:
        key = (p.get("date"), p.get("time"), p.get("apparentTemperature"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def fetch_today_forecast(config):
    if not KMA_FORECAST_SERVICE_KEY:
        return {
            "ok": False,
            "forecastStatus": "SECRET_MISSING",
            "forecastMessage": "KMA_FORECAST_SERVICE_KEY or KMA_AUTH_KEY secret is missing.",
            "forecastItems": []
        }

    request_code = get_forecast_request_code(config)
    areas = get_forecast_areas(config)
    dt = now_kst()
    attempts = []

    for area in areas:
        area_no = area["areaNo"]
        area_name = area["name"]
        for time_text in forecast_time_candidates(dt):
            params = {
                "numOfRows": "100",
                "pageNo": "1",
                "dataType": "JSON",
                "areaNo": area_no,
                "time": time_text,
                "requestCode": request_code,
                "authKey": KMA_FORECAST_SERVICE_KEY
            }
            data, raw_text, full_url, error = request_json_or_xml(FORECAST_URL, params)
            print(f"[DEBUG] Forecast area={area_name}({area_no}) time={time_text} requestCode={request_code}", flush=True)

            if error:
                attempts.append({"areaNo": area_no, "areaName": area_name, "time": time_text, "status": "HTTP_ERROR", "message": error})
                print(f"[WARN] Forecast HTTP error: {error}", flush=True)
                continue

            # Detect API error messages in JSON if present.
            if isinstance(data, dict):
                header = data.get("response", {}).get("header", {}) if isinstance(data.get("response"), dict) else {}
                result_code = str(header.get("resultCode", ""))
                result_msg = str(header.get("resultMsg", ""))
                if result_code and result_code not in ["00", "0", "1"]:
                    attempts.append({"areaNo": area_no, "areaName": area_name, "time": time_text, "status": "API_ERROR", "message": result_msg or result_code})
                    print(f"[WARN] Forecast API error: {result_msg or result_code}", flush=True)
                    continue

            points = parse_living_heat_forecast(data, raw_text, time_text)
            if not points:
                sample = (raw_text or "")[:300].replace("\n", " ")
                attempts.append({"areaNo": area_no, "areaName": area_name, "time": time_text, "status": "NO_VALID_FORECAST", "message": sample})
                print("[WARN] Forecast parse failed: NO_VALID_FORECAST", flush=True)
                continue

            # Prefer today and future points when exact dates are available.
            today = dt.strftime("%Y%m%d")
            now_hhmm = dt.strftime("%H%M")
            filtered = []
            for p in points:
                p_date = str(p.get("date", ""))
                p_time = str(p.get("time", ""))
                if p_date and p_date != today:
                    continue
                if p_time and p_time.isdigit() and len(p_time) == 4 and p_time < now_hhmm:
                    continue
                filtered.append(p)
            if not filtered:
                filtered = points

            max_point = max(filtered, key=lambda x: x["apparentTemperature"])
            return {
                "ok": True,
                "forecastStatus": "OK",
                "forecastMessage": "",
                "forecastItems": filtered,
                "forecastMaxApparentTemperature": max_point["apparentTemperature"],
                "forecastMaxTime": max_point.get("time"),
                "forecastMaxLevel": max_point["level"],
                "forecastAreaNo": area_no,
                "forecastAreaName": area_name,
                "forecastRequestCode": request_code,
                "forecastBaseTime": time_text,
                "forecastBaseDate": time_text[:8] if len(time_text) >= 8 else ""
            }

    return {
        "ok": False,
        "forecastStatus": "NO_VALID_FORECAST",
        "forecastMessage": "생활기상지수 API에서 유효한 체감온도 예보값을 찾지 못했습니다.",
        "forecastItems": [],
        "forecastRequestCode": request_code,
        "forecastAttempts": attempts[:20]
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
            "forecastAreaNo": forecast.get("forecastAreaNo"),
            "forecastAreaName": forecast.get("forecastAreaName"),
            "forecastRequestCode": forecast.get("forecastRequestCode")
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
        "forecastAreaNo": forecast.get("forecastAreaNo"),
        "forecastAreaName": forecast.get("forecastAreaName"),
        "forecastRequestCode": forecast.get("forecastRequestCode"),
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
