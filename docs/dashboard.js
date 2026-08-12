function setText(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = value;
  }
}

function levelClass(level) {
  if (level === "정상") return "normal";
  if (level === "주의") return "caution";
  if (level === "경계") return "warning";
  if (level === "위험") return "danger";
  if (level === "매우위험") return "extreme";

  return "";
}

async function loadDashboard() {
  try {
    const response = await fetch("./current.json?ts=" + Date.now());

    if (!response.ok) {
      throw new Error("current.json not found");
    }

    const data = await response.json();

    setText("siteName", data.siteName || "-");
    setText("address", data.address || "-");

    setText(
      "observedAt",
      "최근 조회: " + (data.observedAt || "-")
    );

    setText(
      "apparentTemperature",
      data.apparentTemperature != null
        ? data.apparentTemperature.toFixed(1) + "℃"
        : "-"
    );

    setText(
      "levelText",
      data.level || "-"
    );

    setText(
      "levelChange",
      "단계변경: " +
      (data.previousLevel || "-") +
      " → " +
      (data.level || "-")
    );

    setText(
      "temperature",
      data.temperature != null
        ? data.temperature + "℃"
        : "-"
    );

    setText(
      "humidity",
      data.humidity != null
        ? data.humidity + "%"
        : "-"
    );

    setText(
      "windSpeed",
      data.windSpeed != null
        ? data.windSpeed + " m/s"
        : "-"
    );

    setText(
      "awsStationName",
      data.awsStationName || data.awsStation || "-"
    );

    const badge = document.getElementById("levelBadge");

    if (badge) {
      badge.textContent = data.level || "확인 중";
      badge.className = "badge " + levelClass(data.level);
    }

    const levelText = document.getElementById("levelText");

    if (levelText) {
      levelText.className = "level " + levelClass(data.level);
    }

  } catch (err) {
    console.error(err);

    setText(
      "observedAt",
      "데이터를 불러오지 못했습니다."
    );
  }
}

loadDashboard();
