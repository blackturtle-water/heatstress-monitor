import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "v0.6.0"

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
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL")
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL",
    "https://blackturtle-water.github.io/heatstress-monitor/"
)

AWS_URL = "https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min"


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
        print(f"[WARN] Failed to read JSON {path}: {exc}")
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_config():
    config = read_json(CONFIG_PATH, None)

    if not isinstance(config, dict):
        raise RuntimeError("config/sites.json is missing or invalid.")

    if "siteName" not in config:
        raise RuntimeError("config/sites.json missing siteName.")

    if "address" not in config:
        raise RuntimeError("config/sites.json missing address.")

    if "awsStations" not in config:
        if "awsStation" in config:
            config["awsStations"] = [
                {
                    "id": str(config["awsStation"]),
                    "name": str(config["awsStation"])
                }
            ]
        else:
            raise RuntimeError("config/sites.json missing awsStations.")

    return config


def get_station_list(config):
    result = []

    for item in config.get("awsStations", []):
        if isinstance(item, dict):
            station_id = str(item.get("id", "")).strip()
            station_name = str(item.get("name", station_id)).strip()
        else:
            station_id = str(item).strip()
            station_name = station_id

        if station_id:
            result.append(
                {
                    "id": station_id,
                    "name": station_name or station_id
                }
            )

    if not result:
        raise RuntimeError("No AWS stations configured.")

    return result


def request_text(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"

    try:
        request = urllib.request.Request(
            full_url,
            headers={"User-Agent": "heatstress-monitor/1.0"}
        )

        with urllib.request.urlopen(request, timeout=6) as response:
            raw = response.read()

        for encoding in ["utf-8", "euc-kr", "cp949"]:
            try:
                return raw.decode(encoding), full_url, None
            except UnicodeDecodeError:
                pass

        return raw.decode("utf-8", errors="replace"), full_url, None

    except Exception as exc:
        return "", full_url, f"HTTP_ERROR: {exc}"


def parse_float(value):
    if value is None:
        return None

    text = str(value).strip()

    if text in [
        "",
        "-",
        "-9",
        "-9.0",
        "-99",
        "-99.0",
        "-99.9",
        "-999",
        "-999.0",
        "-999.9",
        "-9999",
        "-9999.0",
        "-9999.9"
    ]:
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

        if not line:
            continue

        if line.startswith("#"):
            continue

        parts = line.split()

        if len(parts) < 18:
            continue

        time_text = parts[0]
        station_text = parts[1]

        if not time_text.isdigit():
            continue

        if station_text != str(station_id):
            continue

        rows.append(parts)

    if not rows:
        return None, "NO_DATA_ROWS"

    for row in reversed(rows):
        observed_time = row[0]

        wind_speed = parse_float(row[7]) or parse_float(row[3])
        temperature = parse_float(row[8])
        humidity = parse_float(row[14])

        if temperature is None or humidity is None:
            continue

        return {
            "observedTime": observed_time,
            "temperature": temperature,
            "humidity": humidity,
            "windSpeed": wind_speed,
            "rawRow": row
        }, None

    return None, "NO_VALID_OBSERVATION_VALUES"


def fetch_weather(config):
    if not KMA_AUTH_KEY:
        return {
            "ok": False,
            "apiStatus": "SECRET_MISSING",
            "apiMessage": "KMA_AUTH_KEY secret is missing.",
            "attempts": []
        }

    stations = get_station_list(config)
    current_time = now_kst()
    minutes_list = [10, 20, 30, 60]
    attempts = []

    for station in stations:
        station_id = station["id"]
        station_name = station["name"]

        for minutes_back in minutes_list:
            target_time = current_time - timedelta(minutes=minutes_back)
            tm2 = target_time.strftime("%Y%m%d%H%M")

            params = {
                "tm2": tm2,
                "stn": station_id,
                "disp": "0",
                "help": "1",
                "authKey": KMA_AUTH_KEY
            }

            text, full_url, error = request_text(AWS_URL, params)

            print(f"[DEBUG] AWS station={station_name}({station_id}) tm2={tm2}", flush=True)

            if error:
                print(f"[WARN] {error}", flush=True)
                attempts.append(
                    {
                        "stationId": station_id,
                        "stationName": station_name,
                        "tm2": tm2,
                        "status": "HTTP_ERROR",
                        "message": error
                    }
                )
                continue

            parsed, parse_error = parse_aws_response(text, station_id)

            if parsed:
                return {
                    "ok": True,
                    "apiStatus": "OK",
                    "apiMessage": "",
                    "stationId": station_id,
                    "stationName": station_name,
                    "observedTime": parsed["observedTime"],
                    "temperature": parsed["temperature"],
                    "humidity": parsed["humidity"],
                    "windSpeed": parsed["windSpeed"],
                    "url": full_url
                }

            print(f"[WARN] Parse failed: {parse_error}", flush=True)
            attempts.append(
                {
                    "stationId": station_id,
                    "stationName": station_name,
                    "tm2": tm2,
                    "status": "NO_VALID_DATA",
                    "message": parse_error
                }
            )

    return {
        "ok": False,
        "apiStatus": "NO_DATA",
        "apiMessage": "모든 AWS 후보 관측소에서 유효한 기온/습도 값을 찾지 못했습니다.",
        "attempts": attempts[:30]
    }


def c_to_f(value):
    return value * 9 / 5 + 32


def f_to_c(value):
    return (value - 32) * 5 / 9


def calculate_heat_index_celsius(temperature_c, humidity):
    if temperature_c is None or humidity is None:
        return None

    temperature_f = c_to_f(temperature_c)

    if temperature_f < 80:
        simple_f = 0.5 * (
            temperature_f
            + 61.0
            + ((temperature_f - 68.0) * 1.2)
            + (humidity * 0.094)
        )
        heat_index_f = (simple_f + temperature_f) / 2
        return round(f_to_c(heat_index_f), 1)

    heat_index_f = (
        -42.379
        + 2.04901523 * temperature_f
        + 10.14333127 * humidity
        - 0.22475541 * temperature_f * humidity
        - 0.00683783 * temperature_f * temperature_f
        - 0.05481717 * humidity * humidity
        + 0.00122874 * temperature_f * temperature_f * humidity
        + 0.00085282 * temperature_f * humidity * humidity
        - 0.00000199 * temperature_f * temperature_f * humidity * humidity
    )

    if humidity < 13 and 80 <= temperature_f <= 112:
        adjustment = ((13 - humidity) / 4) * math.sqrt((17 - abs(temperature_f - 95)) / 17)
        heat_index_f -= adjustment

    if humidity > 85 and 80 <= temperature_f <= 87:
        adjustment = ((humidity - 85) / 10) * ((87 - temperature_f) / 5)
        heat_index_f += adjustment

    return round(f_to_c(heat_index_f), 1)


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
    ranks = {
        "데이터없음": -1,
        "정상": 0,
        "주의": 1,
        "경계": 2,
        "위험": 3,
        "매우위험": 4
    }

    return ranks.get(level, -1)


def load_last_state():
    default = {
        "level": "정상",
        "apparentTemperature": None,
        "observedAt": None,
        "temperature": None,
        "humidity": None,
        "windSpeed": None,
        "awsStation": None,
        "awsStationName": None,
        "awsObservedTime": None,
        "regularReports": {}
    }

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
        return "📋 [온열질환 정기보고] 08:00 현황", [
            "금일 온열질환 모니터링을 시작합니다.",
            "현재 체감온도와 작업환경을 확인해 주세요."
        ]

    if reason == "regular_13":
        return "📋 [온열질환 정기보고] 13:00 현황", [
            "오후 작업 전 체감온도와 작업환경을 확인해 주세요.",
            "폭염 단계가 상승하면 추가 알림이 발송됩니다."
        ]

    if level == "정상":
        return "✅ [온열질환 정상복귀] 체감온도 31℃ 미만", [
            "현재 체감온도는 31℃ 미만입니다.",
            "다만 작업 전 수분공급과 휴식 관리는 계속 유지해 주세요."
        ]

    if level == "주의":
        return "🟡 [온열질환 주의] 체감온도 31℃ 이상", [
            "시원하고 깨끗한 물을 충분히 제공해 주세요.",
            "폭염작업 시 적절한 휴식을 부여해 주세요.",
            "작업자 건강상태를 주기적으로 확인해 주세요.",
            "냉방·통풍장치 및 그늘막을 활용해 주세요."
        ]

    if level == "경계":
        return "🟠 [온열질환 경계] 체감온도 33℃ 이상", [
            "폭염작업 시 매 2시간 이내 20분 이상 휴식을 부여해 주세요.",
            "작업시간대 조정 또는 옥외작업 단축을 검토해 주세요.",
            "폭염 집중 시간대 노출을 최소화해 주세요.",
            "온열질환 민감군 작업자
