function setText(id, value) {
  const el = document.getElementById(id);

  if (el) {
    el.textContent = value;
  }
}

function formatNumber(value, digits = 1) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }

  const number = Number(value);

  if (Number.isNaN(number)) {
    return "-";
  }

  return number.toFixed(digits);
}

function levelClass(level) {
  if (level === "정상") return "normal";
  if (level === "주의") return "caution";
  if (level === "경계") return "warning";
  if (level === "위험") return "danger";
  if (level === "매우위험") return "extreme";

  return "";
}

function formatObservedTime(value) {
  if (!value) return "-";

  const text = String(value);

  if (text.length === 12) {
    return (
      text.slice(0, 4) + "-" +
      text.slice(4, 6) + "-" +
      text.slice(6, 8) + " " +
      text.slice(8, 10) + ":" +
      text.slice(10, 12)
    );
  }

  return text;
}

async function loadDashboard() {
  try {
    const response = await fetch("./current.json?ts=" + Date.now());

    if (!response.ok) {
      throw new Error("current.json not found");
    }

    const data = await response.json();

    const level = data.level || "-";
    const badgeClass = levelClass(level);

    setText("siteName", data.siteName || "-");
    setText("address", data.address || "-");

    setText(
      "observedAt",
      "최근 조회: " + (data.observedAt || "-")
    );

    setText(
      "apparentTemperature",
      data.apparentTemperature !== null && data.apparentTemperature !== undefined
        ? formatNumber(data.apparentTemperature, 1) + "℃"
        : "-"
    );

    setText("levelText", level);

    setText(
      "levelChange",
      "단계변경: " +
      (data.previousLevel || "-") +
      " → " +
      (data.level || "-")
    );

    setText(
      "temperature",
      data.temperature !== null && data.temperature !== undefined
        ? formatNumber(data.temperature, 1) + "℃"
        : "-"
    );

    setText(
      "humidity",
      data.humidity !== null && data.humidity !== undefined
        ? formatNumber(data.humidity, 1) + "%"
        : "-"
    );

    setText(
      "windSpeed",
      data.windSpeed !== null && data.windSpeed !== undefined
        ? formatNumber(data.windSpeed, 1) + " m/s"
        : "-"
    );

    setText(
      "awsStationName",
      data.awsStationName
        ? data.awsStationName + " (" + (data.awsStation || "-") + ")"
        : (data.awsStation || "-")
    );

    setText(
      "awsObservedTime",
      formatObservedTime(data.awsObservedTime)
    );

    setText("apiStatus", data.apiStatus || "-");
    setText("apiMessage", data.apiMessage || "-");
    setText("teamsNotified", data.teamsNotified ? "발송" : "미발송");

    const badge = document.getElementById("levelBadge");

    if (badge) {
      badge.textContent = level;
      badge.className = "badge " + badgeClass;
    }

    const levelText = document.getElementById("levelText");

    if (levelText) {
      levelText.className = "level " + badgeClass;
    }

  } catch (err) {
    console.error(err);

    setText("observedAt", "데이터를 불러오지 못했습니다.");
    setText("apiStatus", "LOAD_ERROR");
    setText("apiMessage", String(err));
  }
}

loadDashboard();
