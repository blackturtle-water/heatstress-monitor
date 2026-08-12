import csv
import json
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

BASE_SENSIBLE_TEMP_URL = "https://apihub.kma.go.kr/api/typ02/openApi/LivingWthrIdxServiceV3/getSenTaIdxV3"


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

    required_keys = ["siteName", "address", "areaNo"]
    missing = [key for key in required_keys if key not in config]

    if missing:
        raise RuntimeError(f"config/sites.json missing keys: {missing}")

    if "requestCode" not in config:
        config["requestCode"] = "A48"

    return config


def now_kst():
    return datetime.now(KST)


def format_kma_time(dt):
    return dt.strftime("%Y%m%d%H")


def http_get_json(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"

    last_error = None

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                full_url,
                headers={
                    "User-Agent": "heatstress-monitor/1.0"
                }
            )

            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read().decode("utf-8", errors="replace")

            try:
                return json.loads(raw), raw, full_url, None
            except json.JSONDecodeError as e:
                return None, raw, full_url, f"JSON_DECODE_ERROR: {e}"

        except Exception as e:
            last_error = str(e)
            print(f"[WARN] HTTP request failed. attempt={attempt}/3 error={last_error}")

            if attempt < 3:
                time.sleep(3 * attempt)

    return None, "", full_url, f"HTTP_ERROR: {last_error}"


def extract_first_number_from_text(value):
    if value is None:
        return None

    text = str(value).strip()
    candidate = ""

    for ch in text:
        if ch.isdigit() or ch in [".", "-"]:
            candidate += ch
        elif candidate:
            break

    if not candidate:
        return None

    try:
        return float(candidate)
    except ValueError:
        return None


def find_temperature_value(obj):
    """
    기상청 체감온도 API 응답 구조가 실제 응답마다 다를 수 있어
    JSON 전체에서 체감온도 후보값을 최대한 안전하게 찾는다.
    """

    preferred_keys = [
        "h3", "h6", "h9", "h12", "h15", "h18", "h21", "h24",
        "sensibleTemperature",
        "sensorytem",
        "senTa",
        "ta",
        "value",
        "idx",
        "today",
        "tomorrow",
        "theDayAfterTomorrow"
    ]

    found = []

    def walk(x):
        if isinstance(x, dict):
            for key in preferred_keys:
                if key in x:
                    val = extract_first_number_from_text(x.get(key))
                    if val is not None:
                        found.append((key, val))

            for v in x.values():
                walk(v)

        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)

    if found:
        return found[0][1], found

    return None, found


def get_heat_index(config):
    if not KMA_AUTH_KEY:
        return {
            "apparentTemperature": None,
            "kmaTime": format_kma_time(now_kst()),
            "requestCode": config.get("requestCode", "A48"),
            "raw": None,
            "url": None,
            "candidates": [],
            "apiStatus": "SECRET_MISSING",
            "apiMessage": "KMA_AUTH_KEY secret is missing.",
            "attempts": []
        }

    current_time = now_kst()

    primary_code = config.get("requestCode", "A48")

    request_codes = [primary_code]

    for code in ["A48", "A49", "A47", "A44", "A45", "A46"]:
        if code not in request_codes:
            request_codes.append(code)

    attempts = []

    for hours_back in range(0, 25):
        target_time = current_time - timedelta(hours=hours_back)
        kma_time = format_kma_time(target_time)

        for request_code in request_codes:
            params = {
                "numOfRows": "10",
                "pageNo": "1",
                "dataType": "JSON",
                "areaNo": config["areaNo"],
                "time": kma_time,
                "requestCode": request_code,
                "authKey": KMA_AUTH_KEY
            }

            data, raw, full_url, error = http_get_json(BASE_SENSIBLE_TEMP_URL, params)

            if error:
                attempts.append({
                    "time": kma_time,
                    "requestCode": request_code,
                    "status": "HTTP_ERROR",
                    "message": error
                })
                continue

            if data is None:
                attempts.append({
                    "time": kma_time,
                    "requestCode": request_code,
                    "status": "NOT_JSON",
                    "message": raw[:200]
                })
                continue

            header = data.get("response", {}).get("header", {})
            result_code = str(header.get("resultCode", ""))
            result_msg = str(header.get("resultMsg", ""))

            if result_code == "03" or result_msg == "NO_DATA":
                attempts.append({
                    "time": kma_time,
                    "requestCode": request_code,
                    "status": "NO_DATA",
                    "message": result_msg
                })
                continue

            apparent_temp, candidates = find_temperature_value(data)

            if apparent_temp is not None:
                return {
                    "apparentTemperature": apparent_temp,
                    "kmaTime": kma_time,
                    "requestCode": request_code,
                    "raw": data,
                    "url": full_url,
                    "candidates": candidates,
                    "apiStatus": "OK",
                    "apiMessage": result_msg
                }

            attempts.append({
                "time": kma_time,
                "requestCode": request_code,
                "status": "NO_TEMPERATURE_VALUE",
                "message": result_msg,
                "sample": json.dumps(data, ensure_ascii=False)[:500]
            })

    has_http_error = any(item.get("status") == "HTTP_ERROR" for item in attempts)

    if has_http_error:
        api_status = "TIMEOUT_OR_HTTP_ERROR"
        api_message = "기상청 API 호출 중 타임아웃 또는 HTTP 오류 발생"
    else:
        api_status = "NO_DATA"
        api_message = "최근 24시간 내 체감온도 데이터 없음"

    return {
        "apparentTemperature": None,
        "kmaTime": format_kma_time(current_time),
        "requestCode": primary_code,
        "raw": None,
        "url": None,
        "candidates": [],
        "apiStatus": api_status,
        "apiMessage": api_message,
        "attempts": attempts
    }


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
        "apiMessage"
    ]

    with open(HISTORY_PATH, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)


def build_message(config, current, previous_level):
    level = current["level"]
    temp = current["apparentTemperature"]
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
        title = "⚪ [온열질환 데이터 없음] 체감온도 조회 실패"
        actions = [
            "기상청 체감온도 데이터가 현재 조회되지 않았습니다.",
            "대시보드의 API 상태를 확인해 주세요."
        ]

    direction = "단계변경"

    if level_rank(level) > level_rank(previous_level):
        direction = "단계상승"
    elif level_rank(level) < level_rank(previous_level):
        direction = "단계하향"

    bullet_actions = "\n".join([f"- {item}" for item in actions])

    temp_text = "-" if temp is None else f"{temp:.1f}℃"

    text = f"""
{title}

📍 지역: {config["siteName"]}
🏭 주소: {config["address"]}
🕒 조회시각: {observed_at}

🌡 현재 체감온도: {temp_text}
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

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                TEAMS_WEBHOOK_URL,
                data=data,
                headers={
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=60) as response:
                status = response.status
                body = response.read().decode("utf-8", errors="replace")

            print(f"Teams response status: {status}")
            print(f"Teams response body: {body[:300]}")

            return 200 <= status < 300

        except Exception as e:
            last_error = str(e)
            print(f"[WARN] Teams webhook failed. attempt={attempt}/3 error={last_error}")

            if attempt < 3:
                time.sleep(3 * attempt)

    print(f"[WARN] Teams notification failed after retries: {last_error}")
    return False


def should_notify(previous_level, current_level):
    """
    Teams 알림 정책

    1. 데이터없음이면 알림 안 보냄
    2. 정상 유지이면 알림 안 보냄
    3. 같은 단계 유지이면 알림 안 보냄
    4. 단계가 바뀌면 알림 보냄
       - 정상 → 주의
       - 주의 → 경계
       - 경계 → 위험
       - 위험 → 경계
       - 주의 → 정상
       - 기타 단계 상승/하향 포함
    """

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

    heat = get_heat_index(config)
    apparent_temp = heat["apparentTemperature"]

    current_level = decide_level(apparent_temp)
    previous_level = last_state.get("level", "정상")

    notify = should_notify(previous_level, current_level)

    current = {
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "areaNo": config["areaNo"],
        "requestCode": heat.get("requestCode", config.get("requestCode", "A48")),
        "apparentTemperature": apparent_temp,
        "temperature": None,
        "humidity": None,
        "windSpeed": None,
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": notify,
        "teamsNotified": False,
        "apiStatus": heat.get("apiStatus", ""),
        "apiMessage": heat.get("apiMessage", ""),
        "kmaTime": heat.get("kmaTime", "")
    }

    if apparent_temp is None:
        print("KMA heat index data is not available.")
        print(json.dumps({
            "apiStatus": heat.get("apiStatus"),
            "apiMessage": heat.get("apiMessage"),
            "attemptsSample": heat.get("attempts", [])[:10]
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
            "observedAt": observed_at
        })
    else:
        print("last_state.json was not updated because current level is 데이터없음.")

    append_history({
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "apparentTemperature": "" if apparent_temp is None else apparent_temp,
        "temperature": "",
        "humidity": "",
        "windSpeed": "",
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": "Y" if notify else "N",
        "teamsNotified": "Y" if current["teamsNotified"] else "N",
        "apiStatus": current["apiStatus"],
        "apiMessage": current["apiMessage"]
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
                "areaNo": "3114064000",
                "requestCode": "A48",
                "apparentTemperature": None,
                "temperature": None,
                "humidity": None,
                "windSpeed": None,
                "level": "데이터없음",
                "previousLevel": "알수없음",
                "levelChanged": False,
                "teamsNotified": False,
                "apiStatus": "SCRIPT_ERROR",
                "apiMessage": str(e),
                "kmaTime": ""
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
                "apiMessage": str(e)
            })

            print("Script error was recorded to dashboard data.")
            print(json.dumps(error_status, ensure_ascii=False, indent=2))

        except Exception as inner_e:
            print(f"[ERROR] Failed to write error status: {inner_e}", file=sys.stderr)

        sys.exit(0)
