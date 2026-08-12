import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sites.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

CURRENT_PATH = DATA_DIR / "current.json"
HISTORY_PATH = DATA_DIR / "history.csv"
LAST_STATE_PATH = DATA_DIR / "last_state.json"
DASHBOARD_DATA_PATH = DOCS_DIR / "current.json"

KMA_AUTH_KEY = os.environ.get("KMA_AUTH_KEY")
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL")

DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL",
    "https://blackturtle-water.github.io/heatstress-monitor/"
)

AWS_URL = "https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)


def safe_load_json(path, default_value):
    if not path.exists():
        return default_value

    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return default_value
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] Invalid JSON file: {path} / {e}")
        return default_value


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError("config/sites.json file is missing.")

    config = safe_load_json(CONFIG_PATH, None)

    if not isinstance(config, dict):
        raise RuntimeError("config/sites.json is not valid JSON object.")

    required_keys = ["siteName", "address"]
    missing = [key for key in required_keys if key not in config]

    if missing:
        raise RuntimeError(f"config/sites.json missing keys: {missing}")

    if "awsStations" not in config:
        if "awsStation" in config:
            config["awsStations"] = [
                {
                    "id": str(config["awsStation"]),
                    "name": str(config["awsStation"])
                }
            ]
        else:
            raise RuntimeError("config/sites.json requires awsStations or awsStation.")

    return config


def now_kst():
    return datetime.now(KST)


def format_aws_time(dt):
    return dt.strftime("%Y%m%d%H%M")


def http_get_text(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"

    try:
        req = urllib.request.Request(
            full_url,
            headers={"User-Agent": "heatstress-monitor/1.0"}
        )

        with urllib.request.urlopen(req, timeout=6) as response:
            raw_bytes = response.read()

        for enc in ["utf-8", "euc-kr", "cp949"]:
            try:
                text = raw_bytes.decode(enc)
                return text, full_url, None
            except UnicodeDecodeError:
                continue

        text = raw_bytes.decode("utf-8", errors="replace")
        return text, full_url, None

    except Exception as e:
        return "", full_url, f"HTTP_ERROR: {e}"


def to_float(value):
    if value is None:
        return None

    text = str(value).strip()

    missing_values = [
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
    ]

    if text in missing_values:
        return None

    try:
        number = float(text)
    except ValueError:
        return None

    if number <= -50:
        return None

    return number


def is_data_row(tokens, station_id):
    if len(tokens) < 2:
        return False

    if not tokens[0].isdigit():
        return False

    if len(tokens[0]) not in [10, 12]:
        return False

    return tokens[1] == str(station_id)


def parse_aws_text(text, station_id):
    if not text or not text.strip():
        return None, "EMPTY_RESPONSE"

    lines = text.splitlines()

    header_tokens = None
    data_rows = []

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#"):
            candidate = line.lstrip("#").strip()
            tokens = candidate.split()

            if "YYMMDDHHMI" in tokens and "STN" in tokens and "TA" in tokens and "HM" in tokens:
                header_tokens = tokens

            continue

        tokens = line.split()

        if is_data_row(tokens, station_id):
            data_rows.append(tokens)

    if not data_rows:
        sample = text[:800].replace("\n", " ")
        return None, f"NO_DATA_ROWS: {sample}"

    def parse_row(row):
        observed_time = row[0]

        temperature = None
        humidity = None
        wind_speed = None

        if header_tokens:
            def get_by_key(*keys):
                for key in keys:
                    if key in header_tokens:
                        idx = header_tokens.index(key)
                        if idx < len(row):
                            return to_float(row[idx])
                return None

            temperature = get_by_key("TA")
            humidity = get_by_key("HM")
            wind_speed = get_by_key("WS10", "WS1")

        if len(row) > 14:
            if wind_speed is None:
                wind_speed = to_float(row[7]) or to_float(row[3])

            if temperature is None:
                temperature = to_float(row[8])

            if humidity is None:
                humidity = to_float(row[14])

        if temperature is None or humidity is None:
            return None

        return {
            "observedTime": observed_time,
            "temperature": temperature,
            "humidity": humidity,
            "windSpeed": wind_speed,
            "rawRow": row
        }

    for row in reversed(data_rows):
        parsed = parse_row(row)
        if parsed:
            return parsed, None

    last_row = " ".join(data_rows[-1]) if data_rows else ""
    return None, f"NO_VALID_OBSERVATION_VALUES: last_row={last_row}"


def get_station_list(config):
    stations = []

    for item in config.get("awsStations", []):
        if isinstance(item, dict):
            station_id = str(item.get("id", "")).strip()
            station_name = str(item.get("name", station_id)).strip()

            if station_id:
                stations.append(
                    {
                        "id": station_id,
                        "name": station_name or station_id
                    }
                )
        else:
            station_id = str(item).strip()

            if station_id:
                stations.append(
                    {
                        "id": station_id,
                        "name": station_id
                    }
                )

    if not stations:
        raise RuntimeError("No AWS stations configured.")

    return stations


def get_aws_weather(config):
    if not KMA_AUTH_KEY:
        return {
            "temperature": None,
            "humidity": None,
            "windSpeed": None,
            "observedTime": None,
            "apiStatus": "SECRET_MISSING",
            "apiMessage": "KMA_AUTH_KEY secret is missing.",
            "url": None,
            "attempts": [],
            "stationId": None,
            "stationName": None
        }

    current_time = now_kst()
    stations = get_station_list(config)
    attempts = []

    minutes_back_list = [10, 20, 30, 60]

    for station in stations:
        station_id = station["id"]
        station_name = station["name"]

        for minutes_back in minutes_back_list:
            target_time = current_time - timedelta(minutes=minutes_back)
            tm2 = format_aws_time(target_time)

            params = {
                "tm2": tm2,
                "stn": station_id,
                "disp": "0",
                "help": "1",
                "authKey": KMA_AUTH_KEY
            }

            text, full_url, error = http_get_text(AWS_URL, params)

            print(f"[DEBUG] AWS request station={station_name}({station_id}), tm2={tm2}")

            if error:
                attempts.append({
                    "stationId": station_id,
                    "stationName": station_name,
                    "tm2": tm2,
                    "status": "HTTP_ERROR",
                    "message": error
                })
                print(f"[WARN] AWS HTTP error: {error}")
                continue

            parsed, parse_error = parse_aws_text(text, station_id)

            if parsed:
                return {
                    "temperature": parsed["temperature"],
                    "humidity": parsed["humidity"],
                    "windSpeed": parsed["windSpeed"],
                    "observedTime": parsed["observedTime"],
                    "apiStatus": "OK",
                    "apiMessage": "",
                    "url": full_url,
                    "rawRow": parsed["rawRow"],
                    "stationId": station_id,
                    "stationName": station_name
                }

            attempts.append({
                "stationId": station_id,
                "stationName": station_name,
                "tm2": tm2,
                "status": "PARSE_OR_NO_VALID_DATA",
                "message": parse_error
            })

            print(f"[WARN] AWS parse failed: {parse_error}")

    return {
        "temperature": None,
        "humidity": None,
        "windSpeed": None,
        "observedTime": None,
        "apiStatus": "NO_DATA",
        "apiMessage": "모든 AWS 후보 관측소에서 유효한 기온/습도 값을 찾지 못했습니다.",
        "url": None,
        "attempts": attempts[:30],
        "stationId": None,
        "stationName": None
    }


def c_to_f(celsius):
    return celsius * 9 / 5 + 32


def f_to_c(fahrenheit):
    return (fahrenheit - 32) * 5 / 9


def calculate_heat_index_celsius(temp_c, rh):
    if temp_c is None or rh is None:
        return None

    temp_f = c_to_f(temp_c)

    if temp_f < 80:
        simple_hi_f = 0.5 * (
            temp_f
            + 61.0
            + ((temp_f - 68.0) * 1.2)
            + (rh * 0.094)
        )
        hi_f = (simple_hi_f + temp_f) / 2
        return round(f_to_c(hi_f), 1)

    hi_f = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 0.00683783 * temp_f * temp_f
        - 0.05481717 * rh * rh
        + 0.00122874 * temp_f * temp_f * rh
        + 0.00085282 * temp_f * rh * rh
        - 0.00000199 * temp_f * temp_f * rh * rh
    )

    if rh < 13 and 80 <= temp_f <= 112:
        adjustment = ((13 - rh) / 4) * math.sqrt((17 - abs(temp_f - 95)) / 17)
        hi_f = hi_f - adjustment

    elif rh > 85 and 80 <= temp_f <= 87:
        adjustment = ((rh - 85) / 10) * ((87 - temp_f) / 5)
        hi_f = hi_f + adjustment

    return round(f_to_c(hi_f), 1)


def decide_level(temp):
    if temp is None:
        return "데이터없음"

    if temp >= 38:
        return "매우위험"
    if temp >= 35:
        return "위험"
    if temp >= 33:
        return "경계"
    if temp >= 31:
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
    default_state = {
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

    state = safe_load_json(LAST_STATE_PATH, default_state)

    if not isinstance(state, dict):
        return default_state

    if "level" not in state:
        state["level"] = "정상"

    if "regularReports" not in state or not isinstance(state["regularReports"], dict):
        state["regularReports"] = {}

    return state


def load_current_state():
    return safe_load_json(CURRENT_PATH, {})


def is_valid_cached_state(state):
    if not isinstance(state, dict):
        return False

    if state.get("apparentTemperature") is None:
        return False

    if state.get("temperature") is None:
        return False

    if state.get("humidity") is None:
        return False

    if state.get("level") in [None, "", "데이터없음"]:
        return False

    return True


def get_last_valid_state(last_state):
    if is_valid_cached_state(last_state):
        return last_state

    current_state = load_current_state()

    if is_valid_cached_state(current_state):
        return current_state

    return None


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


