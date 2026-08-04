window.KA = window.KA || {};

KA.REFRESH_MS = 10000;
KA.N_POINTS = 60;

var SENSOR_META = {
  moisture_pct: { colour: "#52B788", unit: "%" },
  gas_index: { colour: "#8A5A47", unit: "" },
  temp_c: { colour: "#C4A882", unit: "°C" },
  light_lux: { colour: "#EEE9E2", unit: "lx" },
};

var POT_COLOUR = { A: "#52B788", B: "#8A5A47" };

function fetchJson(url) {
  return fetch(url).then(function (r) {
    if (!r.ok) throw new Error("request failed: " + url);
    return r.json();
  });
}

KA.initRawPage = function (pots) {
  var sensors = Object.keys(SENSOR_META);

  function refresh() {
    pots.forEach(function (pot) {
      fetchJson("/api/raw?pot=" + pot + "&n=" + KA.N_POINTS).then(function (rows) {
        sensors.forEach(function (sensor) {
          var container = document.getElementById("chart-" + sensor + "-" + pot);
          if (!container) return;
          var values = rows.map(function (r) { return r[sensor]; });
          var dates = rows.map(function (r) { return r.timestamp; });
          var chart = KA.trendChart(values, dates, POT_COLOUR[pot], SENSOR_META[sensor].unit, false);
          container.innerHTML = "";
          container.appendChild(chart);
        });
      });
    });
  }

  refresh();
  setInterval(refresh, KA.REFRESH_MS);
};

KA.initInferencePage = function (pots) {
  function refresh() {
    pots.forEach(function (pot) {
      fetchJson("/api/inference?pot=" + pot + "&n=" + KA.N_POINTS).then(function (rows) {
        if (!rows.length) return;

        var rsmiValues = rows.map(function (r) { return r.rsmi; });
        var dates = rows.map(function (r) { return r.timestamp; });
        var chartContainer = document.getElementById("chart-rsmi-" + pot);
        var chart = KA.trendChart(rsmiValues, dates, POT_COLOUR[pot], "", false);
        chartContainer.innerHTML = "";
        chartContainer.appendChild(chart);

        var latest = rows[rows.length - 1];
        var flagEl = document.getElementById("flag-" + pot);
        flagEl.innerHTML = "";
        flagEl.className = "ka-flag ka-flag-" + latest.flag;
        var flagName = document.createElement("div");
        flagName.className = "ka-flag-name";
        flagName.textContent = latest.flag;
        var flagMeta = document.createElement("div");
        flagMeta.className = "ka-flag-meta";
        flagMeta.textContent =
          "confidence " + latest.confidence.toFixed(2) +
          " · " + latest.contributing_trend +
          " · " + latest.model_version;
        flagEl.appendChild(flagName);
        flagEl.appendChild(flagMeta);

        var historyEl = document.getElementById("flag-history-" + pot);
        historyEl.innerHTML = "";
        rows.slice(-10).reverse().forEach(function (r) {
          var item = document.createElement("div");
          item.className = "ka-flag-history-item ka-flag-" + r.flag;
          var time = document.createElement("span");
          time.className = "ka-flag-history-time";
          time.textContent = KA.fmtShortDate(r.timestamp);
          var name = document.createElement("span");
          name.textContent = r.flag;
          item.appendChild(time);
          item.appendChild(name);
          historyEl.appendChild(item);
        });
      });
    });
  }

  refresh();
  setInterval(refresh, KA.REFRESH_MS);
};
