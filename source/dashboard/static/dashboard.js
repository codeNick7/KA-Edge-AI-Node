// dashboard.js — Karbon Assure dual-pot live dashboard (v2.0)
window.KA = window.KA || {};

KA.REFRESH_MS = 1000;   // 1 Hz — matches sketch 1-second logging cadence
KA.N_POINTS   = 60;     // rolling 60-second window

// Sensor definitions — shown as raw-value cards + trend charts
// key must match the field names returned by /api/raw
var SENSOR_META = {
  temp_c:       { label: "Temperature",   colour: "#C4A882", unit: "°C" },
  humidity_pct: { label: "Humidity",      colour: "#7BBFE0", unit: "%" },
  pressure_hpa: { label: "Pressure",      colour: "#9B8EA0", unit: " hPa" },
  gas_index:    { label: "Gas (kΩ)",      colour: "#8A5A47", unit: " kΩ" },
  moisture_pct: { label: "Soil Moisture", colour: "#52B788", unit: "%" },
  light_lux:    { label: "Light",         colour: "#EEE9E2", unit: " lx" },
};

var POT_COLOUR = { A: "#52B788", B: "#7BBFE0" };
var POT_LABEL  = { A: "Pot A — 0x76 / A0 (watered)", B: "Pot B — 0x77 / A1 (dry control)" };

// ── helpers ────────────────────────────────────────────────────────────────
function fetchJson(url) {
  return fetch(url).then(function (r) {
    if (!r.ok) throw new Error("request failed: " + url);
    return r.json();
  });
}

function fmt(val, unit) {
  if (val === null || val === undefined || isNaN(val)) return "—";
  return Number(val).toFixed(1) + unit;
}

// ── raw value card (big number + label) ───────────────────────────────────
function updateRawCard(pot, sensor, meta, rows) {
  var el = document.getElementById("raw-" + sensor + "-" + pot);
  if (!el) return;
  var latest = rows.length ? rows[rows.length - 1][sensor] : null;
  // light_lux placeholder: show "—" when value is 0 (BH1750 not wired yet)
  if (sensor === "light_lux" && latest === 0) latest = null;
  el.innerHTML =
    "<span class=\"ka-raw-val\">" + fmt(latest, meta.unit) + "</span>" +
    "<span class=\"ka-raw-label\">" + meta.label + "</span>";
}

// ── Raw page ───────────────────────────────────────────────────────────────
KA.initRawPage = function (pots) {
  var sensors = Object.keys(SENSOR_META);

  // Set descriptive subtitles
  pots.forEach(function (pot) {
    var h2 = document.querySelector("[data-pot='" + pot + "'] h2");
    if (h2) {
      h2.textContent = "Pot " + pot;
      h2.title = POT_LABEL[pot] || "";
    }
    var sub = document.querySelector("[data-pot='" + pot + "'] .ka-pot-subtitle");
    if (sub) sub.textContent = POT_LABEL[pot] || "";
  });

  function refresh() {
    pots.forEach(function (pot) {
      fetchJson("/api/raw?pot=" + pot + "&n=" + KA.N_POINTS)
        .then(function (rows) {
          // Update raw value cards
          sensors.forEach(function (sensor) {
            updateRawCard(pot, sensor, SENSOR_META[sensor], rows);
          });

          // Update trend charts
          sensors.forEach(function (sensor) {
            var container = document.getElementById("chart-" + sensor + "-" + pot);
            if (!container) return;
            var values = rows.map(function (r) { return r[sensor]; });
            var dates  = rows.map(function (r) { return r.timestamp; });
            var chart  = KA.trendChart(values, dates, POT_COLOUR[pot], SENSOR_META[sensor].unit, false);
            container.innerHTML = "";
            container.appendChild(chart);
          });
        })
        .catch(function (e) { console.warn("raw fetch error:", e); });
    });
  }

  refresh();
  setInterval(refresh, KA.REFRESH_MS);
};

// ── Inference page ─────────────────────────────────────────────────────────
KA.initInferencePage = function (pots) {
  function refresh() {
    pots.forEach(function (pot) {
      fetchJson("/api/inference?pot=" + pot + "&n=" + KA.N_POINTS)
        .then(function (rows) {
          if (!rows.length) return;

          var rsmiValues = rows.map(function (r) { return r.rsmi; });
          var dates = rows.map(function (r) { return r.timestamp; });
          var chartContainer = document.getElementById("chart-rsmi-" + pot);
          if (chartContainer) {
            var chart = KA.trendChart(rsmiValues, dates, POT_COLOUR[pot], "", false);
            chartContainer.innerHTML = "";
            chartContainer.appendChild(chart);
          }

          var latest = rows[rows.length - 1];

          var flagEl = document.getElementById("flag-" + pot);
          if (flagEl) {
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
          }

          var historyEl = document.getElementById("flag-history-" + pot);
          if (historyEl) {
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
          }
        })
        .catch(function (e) { console.warn("inference fetch error:", e); });
    });
  }

  refresh();
  setInterval(refresh, KA.REFRESH_MS);
};
