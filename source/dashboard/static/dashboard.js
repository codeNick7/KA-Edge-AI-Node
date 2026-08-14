// dashboard.js — Karbon Assure dual-pot live dashboard (v3.1)
// Features:
//   - Full history loaded on page load (n=0), live tail appended every second.
//   - Time range filters: "Today", "This Week", "Overall".
//   - Corrected pot labels (Pot A: 0x76/A1, Pot B: 0x77/A0).
//   - Status bar with collecting-since date, counts, and NTP sync status.
//   - Download CSV button.
window.KA = window.KA || {};

KA.REFRESH_MS  = 1000;   // 1 Hz — matches sketch 1-second logging cadence
KA.N_LIVE      = 60;     // points fetched on each live tick (tail only)
KA.currentFilter = "overall"; // "today" | "week" | "overall"

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

// Hardware confirmed 2026-08-14: Pot A → A1, Pot B → A0
var POT_COLOUR = { A: "#52B788", B: "#7BBFE0" };
var POT_LABEL  = {
  A: "Pot A — BME688 @0x76 · Soil A1",
  B: "Pot B — BME688 @0x77 · Soil A0",
};

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

function fmtTs(iso) {
  if (!iso) return "—";
  try {
    var d = new Date(iso);
    return d.toLocaleString();
  } catch (e) { return iso; }
}

// ── Time range filtering ───────────────────────────────────────────────────
function _isDateInFilter(isoString, filter) {
  if (!isoString || filter === "overall") return true;
  var d = new Date(isoString).getTime();
  if (isNaN(d)) return true;
  var now = Date.now();
  if (filter === "today") {
    var todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);
    return d >= todayStart.getTime();
  }
  if (filter === "week") {
    var sevenDaysAgo = now - 7 * 24 * 60 * 60 * 1000;
    return d >= sevenDaysAgo;
  }
  return true;
}

function _filterDataset(values, dates, filter) {
  if (filter === "overall" || !dates || !dates.length) {
    return { values: values, dates: dates };
  }
  var filtValues = [];
  var filtDates = [];
  for (var i = 0; i < dates.length; i++) {
    if (_isDateInFilter(dates[i], filter)) {
      filtDates.push(dates[i]);
      filtValues.push(values[i]);
    }
  }
  // If filter produces too few points (e.g. at the start of a day), show available points
  if (filtValues.length < 2 && values.length >= 2) {
    return { values: values, dates: dates };
  }
  return { values: filtValues, dates: filtDates };
}

function _setupFilterListeners(groupId, onFilterChange) {
  var group = document.getElementById(groupId);
  if (!group) return;
  var btns = group.querySelectorAll(".ka-filter-btn");
  btns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      btns.forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      KA.currentFilter = btn.getAttribute("data-filter") || "overall";
      if (typeof onFilterChange === "function") {
        onFilterChange();
      }
    });
  });
}

// ── status bar ─────────────────────────────────────────────────────────────
KA.refreshStatus = function () {
  fetchJson("/api/status").then(function (s) {
    var el = document.getElementById("ka-status-bar");
    if (!el) return;
    var ntp = s.ntp_synced === true  ? "✅ NTP synced"
            : s.ntp_synced === false ? "⚠️ NTP unavailable"
            : "–";
    var offset = (s.ntp_offset_s !== null && s.ntp_offset_s !== undefined)
      ? " (" + (s.ntp_offset_s >= 0 ? "+" : "") + s.ntp_offset_s.toFixed(3) + " s)"
      : "";
    el.innerHTML =
      "<span>📅 Collecting since: <strong>" + fmtTs(s.collecting_since) + "</strong></span>" +
      "<span>· A: <strong>" + s.total_readings_a + "</strong> readings</span>" +
      "<span>· B: <strong>" + s.total_readings_b + "</strong> readings</span>" +
      "<span>· " + ntp + offset + "</span>" +
      "<span>· Last: " + fmtTs(s.last_reading_at) + "</span>";
  }).catch(function () {});
};

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

// ── chart state — per-pot, per-sensor history buffer ──────────────────────
var _chartData = {};   // _chartData[pot][sensor] = {values, dates}

function _ensureChartData(pot, sensor) {
  if (!_chartData[pot]) _chartData[pot] = {};
  if (!_chartData[pot][sensor]) _chartData[pot][sensor] = { values: [], dates: [] };
  return _chartData[pot][sensor];
}

function _appendRows(pot, rows) {
  if (!rows || !rows.length) return;
  var sensors = Object.keys(SENSOR_META);
  sensors.forEach(function (sensor) {
    var buf = _ensureChartData(pot, sensor);
    rows.forEach(function (r) {
      buf.dates.push(r.timestamp);
      buf.values.push(r[sensor]);
    });
  });
}

function _renderCharts(pot) {
  var sensors = Object.keys(SENSOR_META);
  sensors.forEach(function (sensor) {
    var container = document.getElementById("chart-" + sensor + "-" + pot);
    if (!container) return;
    var buf = _ensureChartData(pot, sensor);
    if (!buf.values.length) return;
    var filt = _filterDataset(buf.values, buf.dates, KA.currentFilter);
    var chart = KA.trendChart(filt.values, filt.dates, POT_COLOUR[pot], SENSOR_META[sensor].unit, false);
    container.innerHTML = "";
    container.appendChild(chart);
  });
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

  // Setup time range filter buttons
  _setupFilterListeners("raw-filter-group", function () {
    pots.forEach(_renderCharts);
  });

  // Load full history on page load, then refresh status bar
  KA.refreshStatus();
  pots.forEach(function (pot) {
    fetchJson("/api/raw?pot=" + pot + "&n=0")
      .then(function (rows) {
        _appendRows(pot, rows);
        sensors.forEach(function (sensor) {
          updateRawCard(pot, sensor, SENSOR_META[sensor], rows);
        });
        _renderCharts(pot);
      })
      .catch(function (e) { console.warn("initial history fetch error:", e); });
  });

  // Track last seen timestamp per pot to only append genuinely new rows
  var _lastTs = { A: null, B: null };

  function liveTick() {
    pots.forEach(function (pot) {
      fetchJson("/api/raw?pot=" + pot + "&n=" + KA.N_LIVE)
        .then(function (rows) {
          if (!rows.length) return;

          // Filter only rows newer than the last seen timestamp
          var newRows = rows.filter(function (r) {
            return !_lastTs[pot] || r.timestamp > _lastTs[pot];
          });

          if (newRows.length) {
            _lastTs[pot] = newRows[newRows.length - 1].timestamp;
            _appendRows(pot, newRows);
            // Update latest-value cards with most-recent row
            sensors.forEach(function (sensor) {
              updateRawCard(pot, sensor, SENSOR_META[sensor], rows);
            });
            _renderCharts(pot);
          }
        })
        .catch(function (e) { console.warn("live tick fetch error:", e); });
    });
    // Refresh status bar every 10 seconds
    if ((Date.now() / 1000 | 0) % 10 === 0) KA.refreshStatus();
  }

  setInterval(liveTick, KA.REFRESH_MS);
};

// ── Inference page ─────────────────────────────────────────────────────────
KA.initInferencePage = function (pots) {
  var _infChartData = {}; // _infChartData[pot] = { rsmi: [], dates: [] }
  var _lastInfTs = { A: null, B: null };

  function renderInfChart(pot) {
    var chartContainer = document.getElementById("chart-rsmi-" + pot);
    if (!chartContainer || !_infChartData[pot] || !_infChartData[pot].rsmi.length) return;
    var filt = _filterDataset(_infChartData[pot].rsmi, _infChartData[pot].dates, KA.currentFilter);
    var chart = KA.trendChart(filt.values, filt.dates, POT_COLOUR[pot], "", false);
    chartContainer.innerHTML = "";
    chartContainer.appendChild(chart);
  }

  function updateFlagUI(pot, rows) {
    if (!rows.length) return;
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
  }

  // Setup time range filter buttons
  _setupFilterListeners("inference-filter-group", function () {
    pots.forEach(renderInfChart);
  });

  // Initial load: full history
  pots.forEach(function (pot) {
    _infChartData[pot] = { rsmi: [], dates: [] };
    fetchJson("/api/inference?pot=" + pot + "&n=0")
      .then(function (rows) {
        if (!rows.length) return;
        _lastInfTs[pot] = rows[rows.length - 1].timestamp;
        rows.forEach(function (r) {
          _infChartData[pot].rsmi.push(r.rsmi);
          _infChartData[pot].dates.push(r.timestamp);
        });
        renderInfChart(pot);
        updateFlagUI(pot, rows);
      })
      .catch(function (e) { console.warn("inference initial fetch error:", e); });
  });

  // Live polling: append new points
  function liveTick() {
    pots.forEach(function (pot) {
      fetchJson("/api/inference?pot=" + pot + "&n=" + KA.N_LIVE)
        .then(function (rows) {
          if (!rows.length) return;
          var newRows = rows.filter(function (r) {
            return !_lastInfTs[pot] || r.timestamp > _lastInfTs[pot];
          });
          if (newRows.length) {
            _lastInfTs[pot] = newRows[newRows.length - 1].timestamp;
            newRows.forEach(function (r) {
              _infChartData[pot].rsmi.push(r.rsmi);
              _infChartData[pot].dates.push(r.timestamp);
            });
            renderInfChart(pot);
            updateFlagUI(pot, rows);
          }
        })
        .catch(function (e) { console.warn("inference live tick error:", e); });
    });
  }

  setInterval(liveTick, KA.REFRESH_MS);
};
