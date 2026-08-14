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

VERSION = "v1.5.2"
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
KMA_FORECAST_SERVICE_KEY = os.environ.get("KMA_VILAGE_FCST_AUTH_KEY", "") or os.environ.get("KMA_AUTH_KEY", "") or os.environ.get("KMA_FORECAST_SERVICE_KEY", "")
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

def parse_kma_time_ymdhm(value):
    text = str(value or "").strip()
    if len(text) >= 12 and text[:12].isdigit():
        try:
            return datetime.strptime(text[:12], "%Y%m%d%H%M").replace(tzinfo=KST)
        except Exception:
            return None
    return None


def fmt_kst(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def minutes_between(later, earlier):
    if not later or not earlier:
        return None
    return round((later - earlier).total_seconds() / 60, 1)


def short_api_message(message):
    text = str(message or "")
    if "시간 제한" in text or "TIME_BUDGET" in text:
        return "실측 API 조회가 제한시간을 초과해 마지막 정상 관측값을 표시 중입니다."
    if "timed out" in text:
        return "실측 API 응답 지연으로 마지막 정상 관측값을 표시 중입니다."
    if len(text) > 120:
        return text[:120] + "..."
    return text


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

    # 중요: GitHub Actions가 API 호출에서 오래 멈추지 않도록 전체 시간과 호출 횟수를 제한한다.
    # 기존 v1.4.8은 4개 관측소 x 11개 시간 x 15초까지 걸릴 수 있어 한 실행이 5분 이상 늘어질 수 있었다.
    attempts = []
    dt = now_kst()
    stations = config.get("awsStations", [])
    minute_candidates = [8, 10, 12, 15, 20, 25, 30, 45]
    max_calls = 12
    call_count = 0
    deadline = time.monotonic() + 45

    def should_stop():
        return call_count >= max_calls or time.monotonic() >= deadline

    # 최신 시각 우선, 관측소 fallback 순서 유지.
    for back in minute_candidates:
        for station in stations:
            if should_stop():
                break
            sid = str(station.get("id", ""))
            name = str(station.get("name", sid))
            tm2 = (dt - timedelta(minutes=back)).strftime("%Y%m%d%H%M")
            params = {"tm2": tm2, "stn": sid, "disp": "0", "help": "0", "authKey": KMA_AUTH_KEY}
            call_count += 1
            print(f"[DEBUG] AWS try {call_count}/{max_calls}: {name}({sid}) tm2={tm2} back={back}", flush=True)
            text, _, error = request_text(AWS_URL, params, timeout=4)
            if error:
                attempts.append(f"{name} {tm2} {error}")
                continue
            parsed = parse_aws(text, sid)
            if parsed:
                parsed.update({
                    "ok": True,
                    "apiStatus": "OK",
                    "apiMessage": "",
                    "stationId": sid,
                    "stationName": name,
                    "awsFetchAttempts": call_count,
                })
                print(f"[DEBUG] AWS success: {name}({sid}) observed={parsed.get('observedTime')} attempts={call_count}", flush=True)
                return parsed
            attempts.append(f"NO_DATA {name} {tm2}")
        if should_stop():
            break

    if time.monotonic() >= deadline:
        status = "TIME_BUDGET_EXCEEDED"
        message = "실측 API 조회 시간 제한을 초과했습니다. 마지막 정상 관측값을 표시합니다."
    else:
        status = "NO_DATA"
        message = "; ".join(attempts[-8:])
    return {"ok": False, "apiStatus": status, "apiMessage": message}


def c_to_f(c):
    return c * 9 / 5 + 32


def f_to_c(f):
    return (f - 32) * 5 / 9


def heat_index_c(temp_c, humidity):
    """대한민국 안전보건공단/산업안전포털 여름철 체감온도 산출표 기반 보간 계산.

    여름철 체감온도는 기온과 습도를 고려한다. 여기서는 공개 산출표의
    기온 25~40도, 습도 25~100% 격자값을 기준으로 선형 보간한다.
    산출표 범위 밖은 가장 가까운 경계값으로 고정한다.
    """
    if temp_c is None or humidity is None:
        return None

    table = {
        25: [22.2,23.1,24.0,24.9,25.8,26.7,27.6,28.5,29.5,30.4,31.3,32.2,33.1,34.0,35.0,35.9],
        30: [22.8,23.7,24.6,25.5,26.5,27.4,28.3,29.2,30.2,31.1,32.0,33.0,33.9,34.8,35.8,36.7],
        35: [23.3,24.2,25.2,26.1,27.0,28.0,28.9,29.9,30.8,31.8,32.7,33.7,34.6,35.6,36.5,37.5],
        40: [23.8,24.7,25.7,26.6,27.6,28.5,29.5,30.4,31.4,32.4,33.3,34.3,35.3,36.2,37.2,38.2],
        45: [24.2,25.2,26.1,27.1,28.1,29.0,30.0,31.0,32.0,32.9,33.9,34.9,35.9,36.9,37.8,38.8],
        50: [24.6,25.6,26.6,27.6,28.6,29.5,30.5,31.5,32.5,33.5,34.5,35.4,36.4,37.4,38.4,39.4],
        55: [25.1,26.0,27.0,28.0,29.0,30.0,31.0,32.0,33.0,34.0,35.0,36.0,37.0,38.0,39.0,40.0],
        60: [25.5,26.5,27.5,28.4,29.4,30.4,31.4,32.4,33.5,34.5,35.5,36.5,37.5,38.5,39.5,40.5],
        65: [25.9,26.9,27.9,28.9,29.9,30.9,31.9,32.9,33.9,34.9,35.9,36.9,38.0,39.0,40.0,41.0],
        70: [26.2,27.2,28.2,29.3,30.3,31.3,32.3,33.3,34.3,35.4,36.4,37.4,38.4,39.5,40.5,41.5],
        75: [26.6,27.6,28.6,29.7,30.7,31.7,32.7,33.7,34.8,35.8,36.8,37.8,38.9,39.9,40.9,42.0],
        80: [27.0,28.0,29.0,30.0,31.1,32.1,33.1,34.1,35.2,36.2,37.2,38.3,39.3,40.4,41.4,42.4],
        85: [27.3,28.4,29.4,30.4,31.4,32.5,33.5,34.5,35.6,36.6,37.7,38.7,39.7,40.8,41.9,42.9],
        90: [27.7,28.7,29.7,30.8,31.8,32.9,33.9,34.9,36.0,37.0,38.1,39.1,40.2,41.2,42.3,43.3],
        95: [28.0,29.1,30.1,31.1,32.2,33.2,34.3,35.3,36.4,37.4,38.5,39.5,40.6,41.6,42.7,43.7],
        100:[28.4,29.4,30.5,31.5,32.6,33.6,34.6,35.7,36.7,37.8,38.9,39.9,41.0,42.0,43.1,44.1],
    }
    temps = list(range(25, 41))
    hums = sorted(table.keys())

    T = max(25.0, min(40.0, float(temp_c)))
    RH = max(25.0, min(100.0, float(humidity)))

    # humidity brackets
    h1 = max(h for h in hums if h <= RH)
    h2 = min(h for h in hums if h >= RH)
    # temperature brackets
    t1 = math.floor(T)
    t2 = math.ceil(T)
    t1 = max(25, min(40, t1))
    t2 = max(25, min(40, t2))

    def value_at(h, t):
        return table[h][t - 25]

    if t1 == t2:
        v_h1 = value_at(h1, t1)
        v_h2 = value_at(h2, t1)
    else:
        ratio_t = (T - t1) / (t2 - t1)
        v_h1 = value_at(h1, t1) + (value_at(h1, t2) - value_at(h1, t1)) * ratio_t
        v_h2 = value_at(h2, t1) + (value_at(h2, t2) - value_at(h2, t1)) * ratio_t

    if h1 == h2:
        result = v_h1
    else:
        ratio_h = (RH - h1) / (h2 - h1)
        result = v_h1 + (v_h2 - v_h1) * ratio_h
    return round(result, 1)

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


def cached_forecast_result(cached, status="OK"):
    keys = [
        "forecastItems", "forecastMaxApparentTemperature", "forecastMaxTime", "forecastMaxLevel",
        "forecastMaxTemperature", "forecastMaxHumidity", "forecastBaseDate", "forecastBaseTime",
        "forecastNx", "forecastNy", "forecastGridName"
    ]
    result = {"ok": True, "forecastStatus": status, "forecastMessage": ""}
    for key in keys:
        if key in cached:
            result[key] = cached.get(key)
    result["forecastSource"] = "CACHE"
    return result


def fetch_forecast(config):
    if not KMA_FORECAST_SERVICE_KEY:
        cached = read_json(CURRENT_PATH, {})
        if cached.get("forecastMaxApparentTemperature") is not None and cached.get("forecastFormula") == "KOSHA_TABLE_INTERPOLATION":
            return cached_forecast_result(cached)
        return {"ok": False, "forecastStatus": "SECRET_MISSING", "forecastMessage": "KMA_FORECAST_SERVICE_KEY is missing"}
    grid = config.get("forecastGrid", {})
    nx, ny = str(grid.get("nx", "102")), str(grid.get("ny", "83"))
    grid_name = str(grid.get("name", "울산 남구"))
    base_date, base_time = latest_base_time(now_kst())

    cached = read_json(CURRENT_PATH, {})
    if (cached.get("forecastBaseDate") == base_date and cached.get("forecastBaseTime") == base_time
            and cached.get("forecastMaxApparentTemperature") is not None and cached.get("forecastFormula") == "KOSHA_TABLE_INTERPOLATION"):
        return cached_forecast_result(cached)

    params = {
        "pageNo": "1", "numOfRows": "1000", "dataType": "JSON",
        "base_date": base_date, "base_time": base_time, "nx": nx, "ny": ny,
        "authKey": KMA_FORECAST_SERVICE_KEY,
    }
    raw, _, error = request_bytes(FORECAST_URL, params, timeout=10)
    if error:
        if cached.get("forecastMaxApparentTemperature") is not None and cached.get("forecastFormula") == "KOSHA_TABLE_INTERPOLATION":
            return cached_forecast_result(cached)
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
        "forecastFormula": "KOSHA_TABLE_INTERPOLATION",
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
    common = [
        "시원하고 깨끗한 물을 충분히 제공하고, 작업자가 규칙적으로 마시도록 안내합니다.",
        "작업자가 즉시 쉴 수 있는 그늘 또는 냉방 휴게시설을 확보합니다.",
        "무더위 시간대에는 옥외작업을 조정·단축하고, 위험 단계 이상에서는 작업중지를 검토합니다.",
        "어지러움·두통·구토 등 온열질환 의심 증상이 있으면 즉시 작업을 중지하고 시원한 장소 이동, 냉각, 119 신고를 실시합니다.",
    ]
    rest_33 = "체감온도 33℃ 이상 작업장에서는 매 2시간 이내에 20분 이상의 휴식시간을 부여합니다."
    if level == "정상":
        return ["현재는 정상 단계입니다. 물·그늘·휴식 준비상태를 유지합니다."] + common
    if level == "주의":
        return ["주의 단계입니다. 물·그늘·휴식 제공 여부를 현장에서 재확인합니다."] + common
    if level == "경계":
        return ["경계 단계입니다. 민감군과 옥외작업자를 우선 확인하고 작업강도를 낮춥니다.", rest_33] + common
    if level == "위험":
        return ["위험 단계입니다. 무더위 시간대 옥외작업 중지 또는 작업시간 단축을 적극 검토합니다.", rest_33] + common
    if level == "매우위험":
        return ["매우위험 단계입니다. 긴급조치 외 옥외작업 중지를 우선 검토합니다.", rest_33] + common
    return ["대시보드 상태를 확인해 주세요."] + common


def send_teams(current, reason):
    if not TEAMS_WEBHOOK_URL:
        return False

    reason_text = {"regular_08": "08:00 정기보고", "regular_13": "13:00 정기보고", "level_change": "단계변경"}.get(reason, "알림")
    current_temp_text = "-" if current.get("apparentTemperature") is None else f"{current.get('apparentTemperature'):.1f}℃"
    current_level = current.get("level", "-")

    forecast_time_raw = str(current.get("forecastMaxTime", ""))
    forecast_time_text = f"{forecast_time_raw[:2]}:{forecast_time_raw[2:4]}" if len(forecast_time_raw) >= 4 else "-"
    if current.get("forecastMaxApparentTemperature") is not None:
        forecast_text = f"오늘 예상 최고 {current['forecastMaxApparentTemperature']:.1f}℃ / {current.get('forecastMaxLevel', '-')} / {forecast_time_text}"
        toast_summary = f"{reason_text} | {current_level} | 현재 {current_temp_text} | 오늘 최고 {current['forecastMaxApparentTemperature']:.1f}℃ {forecast_time_text}"
    else:
        forecast_text = "오늘 예상 최고 예보 미제공"
        toast_summary = f"{reason_text} | {current_level} | 체감온도 {current_temp_text}"

    facts = [
        {"name": "현재 단계", "value": str(current_level)},
        {"name": "현재 체감온도", "value": current_temp_text},
        {"name": "기온/습도/풍속", "value": f"{current.get('temperature', '-'):.1f}℃ / {current.get('humidity', '-'):.1f}% / {current.get('windSpeed') or '-'} m/s"},
        {"name": "예상 최고", "value": forecast_text},
        {"name": "관측지점", "value": f"{current.get('awsStationName', '-')} ({current.get('awsStation', '-')})"},
    ]

    actions_text = "\n".join([f"- {item}" for item in actions_for(current_level)])

    # MessageCard로 전송한다. Adaptive Card는 모바일 푸시에서 'card'로만 보이는 경우가 있어,
    # summary/title/text를 쓰는 MessageCard 형식을 우선 사용한다.
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": toast_summary,
        "themeColor": "0076D7",
        "title": toast_summary,
        "text": f"**{reason_text}**  \n{forecast_text}",
        "sections": [
            {
                "activityTitle": f"온열질환 모니터링 | {current_level}",
                "activitySubtitle": current.get("observedAt", ""),
                "facts": facts,
                "markdown": True,
            },
            {
                "activityTitle": "필요 조치",
                "text": actions_text,
                "markdown": True,
            },
            {
                "activityTitle": "대시보드",
                "text": f"[대시보드 바로가기]({DASHBOARD_URL})  \n{DASHBOARD_URL}",
                "markdown": True,
            },
        ],
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "대시보드 바로가기",
                "targets": [{"os": "default", "uri": DASHBOARD_URL}],
            }
        ],
    }

    try:
        req = urllib.request.Request(
            TEAMS_WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[WARN] Teams failed: {exc}", flush=True)
        return False


def append_history(current):
    fields = ["observedAt", "dataGeneratedAt", "dataAgeMinutes", "siteName", "address", "apparentTemperature", "temperature", "humidity", "windSpeed", "level", "previousLevel", "levelChanged", "notificationReason", "teamsNotified", "apiStatus", "apiMessage", "awsStation", "awsStationName", "awsObservedTime"]
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
    generated_at = fmt_kst(dt)
    if weather.get("ok"):
        apparent = heat_index_c(weather["temperature"], weather["humidity"])
        level = decide_level(apparent)
        observed_dt = parse_kma_time_ymdhm(weather.get("observedTime"))
        data_age = minutes_between(dt, observed_dt)
        current = {
            "observedAt": fmt_kst(observed_dt) or generated_at,
            "dataObservedAt": fmt_kst(observed_dt),
            "dataGeneratedAt": generated_at,
            "dataAgeMinutes": data_age,
            "isStaleData": False,
            "siteName": config["siteName"], "address": config["address"],
            "awsStation": weather["stationId"], "awsStationName": weather["stationName"], "awsObservedTime": weather["observedTime"],
            "apparentTemperature": apparent, "temperature": weather["temperature"], "humidity": weather["humidity"], "windSpeed": weather.get("windSpeed"),
            "level": level, "previousLevel": last.get("level", "정상"), "apiStatus": "OK", "apiMessage": "",
        }
    else:
        cached = read_json(CURRENT_PATH, {})
        current = dict(cached) if cached.get("apparentTemperature") is not None else {"apparentTemperature": None, "temperature": None, "humidity": None, "windSpeed": None, "level": "데이터없음"}
        observed_dt = parse_kma_time_ymdhm(current.get("awsObservedTime"))
        current.update({
            "observedAt": fmt_kst(observed_dt) or current.get("observedAt") or generated_at,
            "dataObservedAt": fmt_kst(observed_dt) or current.get("dataObservedAt"),
            "dataGeneratedAt": generated_at,
            "dataAgeMinutes": minutes_between(dt, observed_dt),
            "isStaleData": True,
            "siteName": config["siteName"], "address": config["address"],
            "previousLevel": last.get("level", "정상"),
            "apiStatus": "STALE_DATA",
            "apiMessage": short_api_message(weather.get("apiMessage", ""))
        })
    current.update({k: v for k, v in forecast.items() if k != "ok"})
    notify, reason = determine_notification(last, current["level"], dt)
    current["levelChanged"] = reason == "level_change"
    current["notificationReason"] = reason
    current["teamsNotified"] = send_teams(current, reason) if notify else False
    write_json(CURRENT_PATH, current)
    write_json(DOCS_CURRENT_PATH, current)
    if current.get("apiStatus") == "OK":
        append_history(current)
    reports = last.get("regularReports", {}) if isinstance(last.get("regularReports"), dict) else {}
    if reason in ["regular_08", "regular_13"] and current["teamsNotified"]:
        reports[regular_key(dt)] = current["observedAt"]
    write_json(LAST_STATE_PATH, {"level": current["level"], "apparentTemperature": current.get("apparentTemperature"), "observedAt": current["observedAt"], "dataGeneratedAt": current.get("dataGeneratedAt"), "temperature": current.get("temperature"), "humidity": current.get("humidity"), "windSpeed": current.get("windSpeed"), "awsStation": current.get("awsStation"), "awsStationName": current.get("awsStationName"), "awsObservedTime": current.get("awsObservedTime"), "regularReports": reports})
    print(json.dumps(current, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
