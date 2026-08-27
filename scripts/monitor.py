import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "v1.6.8"
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sites.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
CURRENT_PATH = DATA_DIR / "current.json"
LAST_STATE_PATH = DATA_DIR / "last_state.json"
HISTORY_PATH = DATA_DIR / "history.csv"
DOCS_CURRENT_PATH = DOCS_DIR / "current.json"
HISTORY_FIELDS = [
    "observedAt", "dataGeneratedAt", "dataAgeMinutes", "siteName", "address",
    "apparentTemperature", "temperature", "humidity", "windSpeed",
    "level", "previousLevel", "levelChanged", "notificationReason", "teamsNotified",
    "apiStatus", "apiMessage", "awsStation", "awsStationName", "awsObservedTime",
]

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


def is_non_working_day(dt):
    """주말 및 공휴일에는 Teams 알림을 보내지 않는다.

    현재 운영용으로 2026년 대한민국 공휴일과 대체공휴일을 반영한다.
    다음 해 운영 전에는 목록을 갱신하거나 공휴일 API 연동으로 전환한다.
    """
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    holidays_2026 = {
        "2026-01-01",  # 신정
        "2026-02-16", "2026-02-17", "2026-02-18",  # 설날 연휴
        "2026-03-01", "2026-03-02",  # 삼일절 및 대체공휴일
        "2026-05-05",  # 어린이날/부처님오신날
        "2026-06-06",  # 현충일
        "2026-08-15", "2026-08-17",  # 광복절 및 대체공휴일
        "2026-09-24", "2026-09-25", "2026-09-26",  # 추석 연휴
        "2026-10-03", "2026-10-05",  # 개천절 및 대체공휴일
        "2026-10-09",  # 한글날
        "2026-12-25",  # 성탄절
    }
    return dt.strftime("%Y-%m-%d") in holidays_2026


def regular_key(dt):
    return f"{dt.strftime('%Y-%m-%d')}_{dt.hour:02d}"


def regular_reason_hour(reason):
    match = re.search(r"regular_(\d{2})", str(reason or ""))
    return int(match.group(1)) if match else None


def report_target(dt):
    """현재 실행이 담당할 정기보고 목표시각을 계산한다.

    각 정기보고의 우선 관측 구간은 목표시각 20분 전부터 20분 후까지다.
    예: 14:40~15:20에 확보된 정상 관측값은 15시 정기보고에 사용한다.
    20분까지 적합한 신규 관측값이 없으면 22분 이후 첫 실행에서
    최근 정상 관측값으로 해당 시간의 정기보고를 진행한다.
    """
    if dt.minute >= 40:
        target = (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        target = dt.replace(minute=0, second=0, microsecond=0)
    return target if 8 <= target.hour <= 17 else None

def regular_key_for(target):
    return f"{target.strftime('%Y-%m-%d')}_{target.hour:02d}"

def determine_notifications(last_state, level, dt, observed_at=None):
    """정기보고와 단계변경을 서로 독립적으로 판정한다.

    정기보고 규칙:
    - 목표시각 -15분 ~ +15분에 관측된 정상 자료가 있으면 즉시 보고
    - +15분까지 적합한 자료가 없으면 보류
    - +20분 이후 첫 실행에서는 가장 최근 정상 자료로 보고
    - Teams 전송 성공 시에만 regularReports 완료 키를 기록
    """
    if is_non_working_day(dt):
        return {
            "regularReason": None,
            "regularKey": None,
            "levelChange": False,
            "blockedReason": "holiday_or_weekend",
        }

    previous = last_state.get("level", "정상")
    level_changed = previous != level and level != "데이터없음"
    reports = last_state.get("regularReports", {})
    target = report_target(dt)
    regular_reason = None
    regular_key_value = None

    if target is not None and level != "데이터없음":
        report_key = regular_key_for(target)
        window_start = target - timedelta(minutes=20)
        preferred_end = target + timedelta(minutes=20)
        fallback_at = target + timedelta(minutes=22)

        observed_dt = None
        if observed_at:
            try:
                observed_dt = datetime.strptime(
                    str(observed_at)[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=KST)
            except Exception:
                observed_dt = None

        fresh_candidate = (
            observed_dt is not None
            and window_start <= observed_dt <= preferred_end
        )
        fallback_due = dt >= fallback_at

        if report_key not in reports and (fresh_candidate or fallback_due):
            regular_reason = f"regular_{target.hour:02d}"
            regular_key_value = report_key

    return {
        "regularReason": regular_reason,
        "regularKey": regular_key_value,
        "levelChange": level_changed,
        "blockedReason": None,
    }

def determine_notification(last_state, level, dt, observed_at=None):
    """기존 호출 호환용. 신규 코드는 determine_notifications를 사용한다."""
    result = determine_notifications(last_state, level, dt, observed_at)
    if result.get("blockedReason"):
        return False, result["blockedReason"]
    if result.get("regularReason"):
        return True, result["regularReason"]
    if result.get("levelChange"):
        return True, "level_change"
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

    report_hour = regular_reason_hour(reason)
    if report_hour is not None and "level_change" in reason:
        reason_text = f"{report_hour:02d}:00 정기보고 / 단계변경"
    elif report_hour is not None:
        reason_text = f"{report_hour:02d}:00 정기보고"
    elif reason == "level_change":
        reason_text = "단계변경"
    else:
        reason_text = "알림"

    current_temp_text = "-" if current.get("apparentTemperature") is None else f"{current.get('apparentTemperature'):.1f}℃"
    current_level = current.get("level", "-")
    level_theme = {
        "정상": "00AA55", "주의": "FACC15", "경계": "FB923C",
        "위험": "EF4444", "매우위험": "991B1B",
    }.get(current_level, "0076D7")
    level_icon = {
        "정상": "🟢", "주의": "🟡", "경계": "🟠",
        "위험": "🔴", "매우위험": "🟥",
    }.get(current_level, "⚪")
    data_basis = "최근 정상 관측자료" if current.get("isStaleData") else "최신 관측자료"

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
        {"name": "실제 관측시각", "value": str(current.get("observedAt", "-"))},
        {"name": "자료 기준", "value": data_basis},
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
        "themeColor": level_theme,
        "title": f"{level_icon} {toast_summary}",
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


def looks_datetime(value):
    text = str(value or "").strip()
    return len(text) >= 16 and text[4:5] == "-" and text[7:8] == "-" and (" " in text or "T" in text)


def number_text(value):
    value = str(value or "").strip()
    try:
        numeric = float(value)
    except Exception:
        return ""
    if numeric <= -50:
        return ""
    return value


def normalize_history_row(row):
    row = list(row)
    if not row or not looks_datetime(row[0]):
        return None

    # Canonical v2 row:
    # observedAt,dataGeneratedAt,dataAgeMinutes,siteName,address,apparentTemperature,...
    if len(row) >= 19 and looks_datetime(row[1]):
        data = {field: (row[i] if i < len(row) else "") for i, field in enumerate(HISTORY_FIELDS)}
    else:
        # Legacy row before dataGeneratedAt/dataAgeMinutes were inserted.
        data = {field: "" for field in HISTORY_FIELDS}
        data.update({
            "observedAt": row[0] if len(row) > 0 else "",
            "siteName": row[1] if len(row) > 1 else "",
            "address": row[2] if len(row) > 2 else "",
            "apparentTemperature": row[3] if len(row) > 3 else "",
            "temperature": row[4] if len(row) > 4 else "",
            "humidity": row[5] if len(row) > 5 else "",
            "windSpeed": row[6] if len(row) > 6 else "",
            "level": row[7] if len(row) > 7 else "",
            "previousLevel": row[8] if len(row) > 8 else "",
            "levelChanged": row[9] if len(row) > 9 else "",
        })
        idx = 10
        if len(row) > idx and (row[idx] == "none" or row[idx] == "stale_data" or row[idx] == "level_change" or row[idx].startswith("regular_")):
            data["notificationReason"] = row[idx]
            idx += 1
        if len(row) > idx:
            data["teamsNotified"] = row[idx]
            idx += 1
        if len(row) > idx:
            data["apiStatus"] = row[idx]
            idx += 1
        if len(row) > idx:
            data["apiMessage"] = row[idx]
            idx += 1
        if len(row) > idx:
            data["awsStation"] = row[idx]
            idx += 1
        if len(row) > idx:
            # Some legacy rows have stationName here, some have awsObservedTime directly.
            if str(row[idx]).isdigit() and len(str(row[idx])) >= 10:
                data["awsObservedTime"] = row[idx]
            else:
                data["awsStationName"] = row[idx]
                idx += 1
                if len(row) > idx:
                    data["awsObservedTime"] = row[idx]

    data["apparentTemperature"] = number_text(data.get("apparentTemperature"))
    data["temperature"] = number_text(data.get("temperature"))
    data["humidity"] = number_text(data.get("humidity"))
    data["windSpeed"] = number_text(data.get("windSpeed"))

    # Keep only valid rows for graph/statistics. Stale/no-data rows duplicate old values and distort charts.
    if not data.get("apparentTemperature"):
        return None
    if data.get("apiStatus") and data.get("apiStatus") != "OK":
        return None
    return {field: data.get(field, "") for field in HISTORY_FIELDS}


def migrate_history_schema():
    if not HISTORY_PATH.exists():
        return
    try:
        with HISTORY_PATH.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except Exception as exc:
        print(f"[WARN] history read failed: {exc}", flush=True)
        return
    if not rows:
        return
    header = rows[0]
    if header == HISTORY_FIELDS:
        return

    normalized = []
    seen = set()
    for raw in rows[1:]:
        item = normalize_history_row(raw)
        if not item:
            continue
        key = item.get("observedAt")
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)

    backup = HISTORY_PATH.with_suffix(".csv.bak")
    try:
        if not backup.exists():
            backup.write_text(HISTORY_PATH.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
    except Exception as exc:
        print(f"[WARN] history backup failed: {exc}", flush=True)

    with HISTORY_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(normalized)
    print(f"[INFO] migrated history.csv rows={len(normalized)}", flush=True)


def history_has_event(observed_at, notification_reason):
    if not observed_at or not HISTORY_PATH.exists():
        return False
    try:
        with HISTORY_PATH.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("observedAt") != observed_at:
                    continue
                existing_reason = row.get("notificationReason", "")
                if notification_reason.startswith("regular_") or notification_reason == "level_change":
                    if existing_reason == notification_reason:
                        return True
                else:
                    return True
        return False
    except Exception:
        return False


def append_history(current):
    migrate_history_schema()
    exists = HISTORY_PATH.exists()
    reason = str(current.get("notificationReason", ""))
    is_successful_regular = reason.startswith("regular_") and bool(current.get("teamsNotified"))

    # 일반 이력은 정상 API 값만 저장한다. 정기보고는 수집 실패 시 직전 정상값을 사용한 사실도 기록한다.
    if current.get("apiStatus") != "OK" and not is_successful_regular:
        return
    if history_has_event(current.get("observedAt"), reason):
        print(f"[INFO] history duplicate skipped: {current.get('observedAt')} / {reason}", flush=True)
        return
    with HISTORY_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: current.get(k, "") for k in HISTORY_FIELDS})


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
    decisions = determine_notifications(last, current["level"], dt, current.get("observedAt"))
    regular_reason = decisions.get("regularReason")
    level_change_due = bool(decisions.get("levelChange"))

    regular_sent = send_teams(current, regular_reason) if regular_reason else False
    level_change_sent = send_teams(current, "level_change") if level_change_due else False

    current["levelChanged"] = level_change_due
    current["regularNotificationReason"] = regular_reason or "none"
    current["regularTeamsNotified"] = regular_sent
    current["levelChangeNotificationReason"] = "level_change" if level_change_due else "none"
    current["levelChangeTeamsNotified"] = level_change_sent
    current["notificationReasons"] = [
        reason for reason, sent in ((regular_reason, regular_sent), ("level_change" if level_change_due else None, level_change_sent))
        if reason and sent
    ]
    if regular_sent and level_change_sent:
        current["notificationReason"] = f"{regular_reason}+level_change"
    elif regular_sent:
        current["notificationReason"] = regular_reason
    elif level_change_sent:
        current["notificationReason"] = "level_change"
    elif decisions.get("blockedReason"):
        current["notificationReason"] = decisions["blockedReason"]
    else:
        current["notificationReason"] = "none"
    current["teamsNotified"] = regular_sent or level_change_sent

    write_json(CURRENT_PATH, current)
    write_json(DOCS_CURRENT_PATH, current)

    # 동일한 관측값에서 두 알림이 발생해도 history.csv에는 별도 행으로 저장한다.
    if regular_sent:
        regular_event = dict(current)
        regular_event["notificationReason"] = regular_reason
        regular_event["teamsNotified"] = True
        regular_event["levelChanged"] = False
        append_history(regular_event)
    if level_change_sent:
        level_event = dict(current)
        level_event["notificationReason"] = "level_change"
        level_event["teamsNotified"] = True
        level_event["levelChanged"] = True
        append_history(level_event)
    if not regular_sent and not level_change_sent:
        ordinary_event = dict(current)
        ordinary_event["notificationReason"] = decisions.get("blockedReason") or "none"
        ordinary_event["teamsNotified"] = False
        append_history(ordinary_event)

    reports = last.get("regularReports", {}) if isinstance(last.get("regularReports"), dict) else {}
    if regular_reason and regular_sent and decisions.get("regularKey"):
        reports[decisions["regularKey"]] = current["observedAt"]
    write_json(LAST_STATE_PATH, {"level": current["level"], "apparentTemperature": current.get("apparentTemperature"), "observedAt": current["observedAt"], "dataGeneratedAt": current.get("dataGeneratedAt"), "temperature": current.get("temperature"), "humidity": current.get("humidity"), "windSpeed": current.get("windSpeed"), "awsStation": current.get("awsStation"), "awsStationName": current.get("awsStationName"), "awsObservedTime": current.get("awsObservedTime"), "regularReports": reports})
    print(json.dumps(current, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
