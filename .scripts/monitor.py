import csv
import json
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

BASE_SENSIBLE_TEMP_URL = "https://apihub.kma.go.kr/api/typ02/openApi/LivingWthrIdxServiceV3/getSenTaIdxV3"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def now_kst():
    return datetime.now(KST)


def format_kma_time(dt):
    return dt.strftime("%Y%m%d%H")


def http_get_json(url, params):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"

    req = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "heatstress-monitor/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")

    try:
        return json.loads(raw), raw, full_url
    except json.JSONDecodeError:
        return None, raw, full_url


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
    기상청 API 응답 구조가 문서/버전에 따라 다를 수 있으므로,
    JSON 전체에서 체감온도로 추정되는 숫자값을 최대한 안전하게 찾는다.
    우선순위:
    1. h3, h6, h9, h12 등 시간별 값
    2. sensibleTemperature, sensorytem, ta 등 체감온도명 유사 키
    3. item 내부 숫자 필드
    """

    preferred_keys = [
        "h3", "h6", "h9", "h12", "h15", "h18", "h21", "h24",
        "sensibleTemperature", "sensorytem", "senTa", "ta", "value", "idx"
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
        raise RuntimeError("KMA_AUTH_KEY secret is missing.")

    current_time = now_kst()
    kma_time = format_kma_time(current_time)

    params = {
        "numOfRows": "10",
        "pageNo": "1",
        "dataType": "JSON",
        "areaNo": config["areaNo"],
        "time": kma_time,
        "requestCode": config.get("requestCode", "A48"),
        "authKey": KMA_AUTH_KEY
    }

    data, raw, full_url = http_get_json(BASE_SENSIBLE_TEMP_URL, params)

    if data is None:
        raise RuntimeError(f"KMA API did not return JSON. Raw response: {raw[:500]}")

    apparent_temp, candidates = find_temperature_value(data)

    if apparent_temp is None:
        raise RuntimeError(
            "Could not extract apparent temperature from KMA response. "
            f"Response sample: {json.dumps(data, ensure_ascii=False)[:1000]}"
        )

    return {
        "apparentTemperature": apparent_temp,
        "kmaTime": kma_time,
        "raw": data,
        "url": full_url,
        "candidates": candidates
    }


def decide_level(temp):
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
        "정상": 0,
        "주의": 1,
        "경계": 2,
        "위험": 3,
        "매우위험": 4
    }
    return ranks.get(level, -1)


def load_last_state():
    if not LAST_STATE_PATH.exists():
        return {
            "level": "정상",
            "apparentTemperature": None,
            "observedAt": None
        }

    with open(LAST_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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
        "teamsNotified"
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
    else:
        title = "🚨 [온열질환 매우위험] 체감온도 38℃ 이상"
        actions = [
            "긴급조치 작업 외 옥외작업 중지를 검토해 주세요.",
            "작업자 상태를 수시로 확인해 주세요.",
            "온열질환 의심자는 즉시 시원한 장소로 이동시켜 주세요.",
            "의식이 없는 경우 즉시 119에 신고해 주세요.",
            "의식이 있는 경우 응급조치 후 증상 개선이 없으면 119에 신고해 주세요."
        ]

    direction = "단계변경"
    if level_rank(level) > level_rank(previous_level):
        direction = "단계상승"
    elif level_rank(level) < level_rank(previous_level):
        direction = "단계하향"

    bullet_actions = "\n".join([f"- {item}" for item in actions])

    text = f"""
{title}

📍 지역: {config["siteName"]}
🏭 주소: {config["address"]}
🕒 조회시각: {observed_at}

🌡 현재 체감온도: {temp:.1f}℃
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

    req = urllib.request.Request(
        TEAMS_WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        status = response.status
        body = response.read().decode("utf-8", errors="replace")

    print(f"Teams response status: {status}")
    print(f"Teams response body: {body[:300]}")

    return 200 <= status < 300


def main():
    ensure_dirs()
    config = load_config()
    last_state = load_last_state()

    observed_at = now_kst().strftime("%Y-%m-%d %H:%M:%S")

    heat = get_heat_index(config)
    apparent_temp = heat["apparentTemperature"]
    current_level = decide_level(apparent_temp)
    previous_level = last_state.get("level", "정상")
    level_changed = current_level != previous_level

    current = {
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "areaNo": config["areaNo"],
        "requestCode": config.get("requestCode", "A48"),
        "apparentTemperature": apparent_temp,
        "temperature": None,
        "humidity": None,
        "windSpeed": None,
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": level_changed,
        "teamsNotified": False
    }

    title = ""
    message = ""

    if level_changed:
        title, message = build_message(config, current, previous_level)
        notified = send_teams_message(title, message)
        current["teamsNotified"] = notified
    else:
        print(f"No level change. Previous={previous_level}, Current={current_level}")

    save_json(CURRENT_PATH, current)
    save_json(DASHBOARD_DATA_PATH, current)

    save_json(LAST_STATE_PATH, {
        "level": current_level,
        "apparentTemperature": apparent_temp,
        "observedAt": observed_at
    })

    append_history({
        "observedAt": observed_at,
        "siteName": config["siteName"],
        "address": config["address"],
        "apparentTemperature": apparent_temp,
        "temperature": "",
        "humidity": "",
        "windSpeed": "",
        "level": current_level,
        "previousLevel": previous_level,
        "levelChanged": "Y" if level_changed else "N",
        "teamsNotified": "Y" if current["teamsNotified"] else "N"
    })

    print("Current status:")
    print(json.dumps(current, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
