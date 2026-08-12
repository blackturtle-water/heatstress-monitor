import csv
import json
import math
import os
import sys
import time
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

    required_keys = ["siteName", "address", "awsStation"]
    missing = [key for key in required_keys if key not in config]

    if missing:
        raise RuntimeError(f"config/sites.json missing keys: {missing}")

    return config


def now_kst():
    return datetime.now(KST)


def format_aws_time(dt):
    return dt.strftime("%Y%m%d%H%M")


def http_get_text(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"

    last_error = None

    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(
                full_url,
                headers={
                    "User-Agent": "heatstress-monitor/1.0"
                }
            )

            with urllib.request.urlopen(req, timeout=20) as response:
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
            last_error = str(e)
            print(f"[WARN] HTTP request failed. attempt={attempt}/2 error={last_error}")

            if attempt < 2:
                time.sleep(3)

    return "", full_url, f"HTTP_ERROR: {last_error}"


def to_float(value):
    if value is None:
        return None

    text = str(value).strip()

    if text in ["", "-9", "-99", "-999", "-9999"]:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def parse_aws_text(text, station):
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

            if "TA" in tokens and "HM" in tokens:
                header_tokens = tokens

            continue

        tokens = line.split()

        if len(tokens) < 5:
            continue

        data_rows.append(tokens)

    if not data_rows:
        sample = text[:800].replace("\n", " ")
        return None, f"NO_DATA_ROWS: {sample}"

    target_rows = []

    for row in data_rows:
        if len(row) >= 2 and row[1] == str(station):
            target_rows.append(row)

    if not target_rows:
        target_rows = data_rows

    latest = target_rows[-1]

    observed_time = latest[0] if len(latest) > 0 else None

    temperature = None
    humidity = None
    wind_speed = None

    if header_tokens:
        def get_by_key(key):
            if key not in header_tokens:
                return None

            idx = header_tokens.index(key)

            if idx >= len(latest):
                return None

            return to_float(latest[idx])

        temperature = get_by_key("TA")
        humidity = get_by_key("HM")
        wind_speed = get_by_key("WS")

    if temperature is None or humidity is None:
        if len(latest) > 13:
            wind_speed = wind_speed if wind_speed is not None else to_float(latest[3])
            temperature = temperature if temperature is not None else to_float(latest[11])
            humidity = humidity if humidity is not None else to_float(latest[13])

    if temperature is None or humidity is None:
        return None, f"FAILED_TO_PARSE_VALUES: row={' '.join(latest)}"

    return {
        "observedTime": observed_time,
        "temperature": temperature,
        "humidity": humidity,
        "windSpeed": wind_speed,
        "rawRow": latest
    }, None


def get_aws_weather(config):
    if not KMA_AUTH_KEY:
        return {
            "temperature": None,
            "humidity": None,
            "windSpeed": None,
            "observedTime": None,
            "apiStatus": "SECRET_MISSING",
            "apiMessage": "KMA_AUTH_KEY secret is missing.",
            "url": None
        }

    station = str(config["awsStation"])
    current_time = now_kst()

    attempts = []

    for minutes_back in [10, 20, 30, 40, 50, 60, 90, 120]:
        target_time = current_time - timedelta(minutes=minutes_back)
        tm2 = format_aws_time(target_time)

        params = {
            "tm1": "",
            "tm2": tm2,
            "stn": station,
            "disp": "0",
            "help": "1",
            "authKey": KMA_AUTH_KEY
        }

        text, full_url, error = http_get_text(AWS_URL, params)

        if error:
            attempts.append({
                "tm2": tm2,
                "status": "HTTP_ERROR",
                "message": error
            })
            continue

        parsed, parse_error = parse_aws_text(text, station)

        if parsed:
            return {
                "temperature": parsed["temperature"],
                "humidity": parsed["humidity"],
                "windSpeed": parsed["windSpeed"],
                "observedTime": parsed["observedTime"],
                "apiStatus": "OK",
                "apiMessage": "",
                "url": full_url,
                "rawRow": parsed["rawRow"]
            }

        attempts.append({
            "tm2": tm2,
            "status": "PARSE_OR_NO_DATA",
            "message": parse_error
        })

    return {
        "temperature": None,
        "humidity": None,
        "windSpeed": None,
        "observedTime": None,
        "apiStatus": "NO_DATA",
        "apiMessage": "최근 AWS 관측자료를 가져오지 못했습니다.",
        "url": None,
        "attempts": attempts[:10]
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
        "observedAt": None
    }

    state = safe_load_json(LAST_STATE_PATH, default_state)

    if not isinstance(state, dict):
        return default_state

    if "level" not in state:
        state["level"] = "정상"

    return state


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def append_history(row):
    file_exists = HISTORY_PATH.exists()

    fields = [
        "observedAt",
        "siteName",
        "address",
        "apparentTemperature",
        "temperature",
        "humidity",
        "windSpeed",
        "level",
        "previousLevel",
        "levelChanged",
        "teamsNotified",
        "apiStatus",
        "apiMessage",
        "awsStation",
        "awsObservedTime"
    ]

    with open(HISTORY_PATH, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)


def build_message(config, current, previous_level):
    level = current["level"]
    temp = current["apparentTemperature"]
    air_temp = current["temperature"]
    humidity = current["humidity"]
    wind_speed = current["windSpeed"]
    observed_at = current["observedAt"]

    if level == "정상":
        title = "✅ [온열질환 정상복귀] 체감온도 31℃ 미만"
        actions = [
            "현재 체감온도는 31℃ 미만입니다.",
            "다만 작업 전 수분공급과 휴식 관리는 계속 유지해 주세요."
        ]
    elif level == "주의":
        title = "🟡 [온열질환 주의] 체감온도 31℃ 이상"
        actions = [
            "시원하고 깨끗한 물을 충분히 제공해 주세요.",
            "폭염작업 시 적절한 휴식을 부여해 주세요.",
            "작업자 건강상태를 주기적으로 확인해 주세요.",
            "냉방·통풍장치 및 그늘막을 활용해 주세요."
        ]
    elif level == "경계":
        title = "🟠 [온열질환 경계] 체감온도 33℃ 이상"
        actions = [
            "폭염작업 시 매 2시간 이내 20분 이상 휴식을 부여해 주세요.",
            "작업시간대 조정 또는 옥외작업 단축을 검토해 주세요.",
            "폭염 집중 시간대 노출을 최소화해 주세요.",
            "온열질환 민감군 작업자를 추가 확인해 주세요."
        ]
    elif level == "위험":
        title = "🔴 [온열질환 위험] 체감온도 35℃ 이상"
        actions = [
            "무더위 시간대 옥외작업 중지를 검토해 주세요.",
            "작업시간 조정 또는 옥외작업 단축을 시행해 주세요.",
            "작업자 건강상태를 수시로 확인해 주세요.",
            "냉각의류, 냉각조끼 등 보냉장구 지급을 검토해 주세요."
        ]
    elif level == "매우위험":
        title = "🚨 [온열질환 매우위험] 체감온도 38℃ 이상"
        actions = [
            "긴급조치 작업 외 옥외작업 중지를 검토해 주세요.",
            "작업자 상태를 수시로 확인해 주세요.",
            "온열질환 의심자는 즉시 시원한 장소로 이동시켜 주세요.",
            "의식이 없는 경우 즉시 119에 신고해 주세요.",
            "의식이 있는 경우 응급조치 후 증상 개선이 없으면 119에 신고해 주세요."
        ]
    else:
        title = "⚪ [온열질환 데이터 없음] AWS 관측자료 조회 실패"
        actions = [
            "AWS 관측자료 또는 체감온도 계산값을 현재 확인하지 못했습니다.",
            "GitHub Actions 로그와 data/current.json 상태를 확인해 주세요."
        ]

    direction = "단계변경"

    if level_rank(level) > level_rank(previous_level):
        direction = "단계상승"
    elif level_rank(level) < level_rank(previous_level):
        direction = "단계하향"

    bullet_actions = "\n".join([f"- {item}" for item in actions])

    temp_text = "-" if temp is None else f"{temp:.1f}℃"
    air_temp_text = "-" if air_temp is None else f"{air_temp:.1f}℃"
    humidity_text = "-" if humidity is None else f"{humidity:.0f}%"
    wind_text = "-" if wind_speed is None else f"{wind_speed:.1f} m/s"

    text = f"""
{title}

📍 지역: {config["siteName"]}
🏭 주소: {config["address"]}
🕒 조회시각: {observed_at}
📡 AWS 지점: {config["awsStation"]}

🌡 체감온도: {temp_text}
🌡 기온: {air_temp_text}
💧 습도: {humidity_text}
🌬 풍속: {wind_text}

📊 {direction}: {previous_level} → {level}

✅ 권고조치
{bullet_actions}
""".strip()

    return title, text


def send_teams_message(title, text):
    if not TEAMS_WEBHOOK_URL:
        print("TEAMS_WEBHOOK_URL secret is missing. Skip Teams notification.")
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
                        {
                            "type": "TextBlock",
                            "text": title,
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": text,
                            "wrap": True
                        }
                    ]
                }
            }
        ]
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_error = None

    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(
                TEAMS_WEBHOOK_URL,
                data=data,
                headers={
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=20) as response:
                status = response.status
                body = response.read().decode("utf-8", errors="replace")

            print(f"Teams response status: {status}")
            print(f"Teams response body: {body[:300]}")

            return 200 <= status < 300

        except Exception as e:
            last_error = str(e)
            print(f"[WARN] Teams webhook failed. attempt={attempt}/2 error={last_error}")

            if attempt < 2:
                time.sleep(3)

    print(f"[WARN] Teams notification failed after retries: {last_error}")
    return False


def should_notify(previous_level, current_level):
    if current_level == "데이터없음":
        return False

    if previous_level == current_level:
        return False

    return True


def main():
    ensure_dirs()
    config = load_config()
    last_state = load_last_state()

    observed_at = now_kst().strftime("%Y-%m-%d %H:%M:%S")

    weather = get_aws_weather(config)

    temperature = weather["temperature"]
    humidity = weather["humidity"]
    wind_speed = weather["windSpeed"]

    apparent_temp = calculate_heat_index_celsius(temperature, humidity)

    current_level = decide_level(apparent_temp)
    previous_level = last_state.get("level", "정상")

    notify = should_notify(previous_level, current_level)

    current = {
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "awsStation": config["awsStation"],
        "awsObservedTime": weather.get("observedTime"),
        "apparentTemperature": apparent_temp,
        "temperature": temperature,
        "humidity": humidity,
        "windSpeed": wind_speed,
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": notify,
        "teamsNotified": False,
        "apiStatus": weather.get("apiStatus", ""),
        "apiMessage": weather.get("apiMessage", "")
    }

    if current_level == "데이터없음":
        print("AWS weather data is not available.")
        print(json.dumps({
            "apiStatus": weather.get("apiStatus"),
            "apiMessage": weather.get("apiMessage"),
            "attemptsSample": weather.get("attempts", [])[:10]
        }, ensure_ascii=False, indent=2))
        print("No Teams notification because current level is 데이터없음.")

    elif notify:
        title, message = build_message(config, current, previous_level)
        notified = send_teams_message(title, message)
        current["teamsNotified"] = notified

    else:
        print(f"No notification. Previous={previous_level}, Current={current_level}")

    save_json(CURRENT_PATH, current)
    save_json(DASHBOARD_DATA_PATH, current)

    if current_level != "데이터없음":
        save_json(LAST_STATE_PATH, {
            "level": current_level,
            "apparentTemperature": apparent_temp,
            "observedAt": observed_at,
            "temperature": temperature,
            "humidity": humidity,
            "windSpeed": wind_speed
        })
    else:
        print("last_state.json was not updated because current level is 데이터없음.")

    append_history({
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "apparentTemperature": "" if apparent_temp is None else apparent_temp,
        "temperature": "" if temperature is None else temperature,
        "humidity": "" if humidity is None else humidity,
        "windSpeed": "" if wind_speed is None else wind_speed,
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": "Y" if notify else "N",
        "teamsNotified": "Y" if current["teamsNotified"] else "N",
        "apiStatus": current["apiStatus"],
        "apiMessage": current["apiMessage"],
        "awsStation": config["awsStation"],
        "awsObservedTime": weather.get("observedTime") or ""
    })

    print("Current status:")
    print(json.dumps(current, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)

        try:
            ensure_dirs()

            error_status = {
                "observedAt": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                "siteName": "유니드 울산공장",
                "address": "울산광역시 남구 상개로 142",
                "awsStation": "954",
                "awsObservedTime": None,
                "apparentTemperature": None,
                "temperature": None,
                "humidity": None,
                "windSpeed": None,
                "level": "데이터없음",
                "previousLevel": "알수없음",
                "levelChanged": False,
                "teamsNotified": False,
                "apiStatus": "SCRIPT_ERROR",
                "apiMessage": str(e)
            }

            save_json(CURRENT_PATH, error_status)
            save_json(DASHBOARD_DATA_PATH, error_status)

            append_history({
                "observedAt": error_status["observedAt"],
                "siteName": error_status["siteName"],
                "address": error_status["address"],
                "apparentTemperature": "",
                "temperature": "",
                "humidity": "",
                "windSpeed": "",
                "level": "데이터없음",
                "previousLevel": "알수없음",
                "levelChanged": "N",
                "teamsNotified": "N",
                "apiStatus": "SCRIPT_ERROR",
                "apiMessage": str(e),
                "awsStation": "954",
                "awsObservedTime": ""
            })

            print("Script error was recorded to dashboard data.")
            print(json.dumps(error_status, ensure_ascii=False, indent=2))

        except Exception as inner_e:
            print(f"[ERROR] Failed to write error status: {inner_e}", file=sys.stderr)

        sys.exit(0)
