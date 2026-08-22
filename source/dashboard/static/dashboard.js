// dashboard.js — Karbon Assure dual-pot live dashboard (v3.2)
// Features:
//   - Full history loaded on page load (n=0), live rolling tail appended every second.
//   - Time range filters: "5 Mins", "15 Mins", "1 Hour", "Today", "This Week", "Overall".
//   - Rolling window dynamically slides as new readings arrive without freezing.
//   - Deduplicated timestamp appending to prevent chart distortion.
//   - Status bar with session metadata and NTP sync indicator.
//   - CSV downloads.
window.KA = window.KA || {};

KA.REFRESH_MS  = 5000;   // 5s auto-refresh cadence
KA.N_LIVE      = 60;     // points fetched on each live tick (tail only)
KA.CHART_MAX_POINTS = 1500; // decimation cap for range-scoped chart fetches (n=0)
KA.currentFilter = "overall"; // "5m" | "15m" | "1h" | "today" | "week" | "overall"


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

// Soil pin mapping corrected 2026-08-15: Pot A → A1, Pot B → A0 (the
// 2026-08-14 "confirmed" A0/A1 mapping was backwards from day one — see
// sketch.ino header and scripts/migrate_soil_pin_swap.py).
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

// Range-scoped, decimated full-history fetch for chart rendering (n=0, plus
// range+max_points so the server bounds the payload to what's actually
// renderable instead of every row ever collected — see real_source.py's
// _slice_by_range/_decimate, 2026-08-22 dashboard perf work). Used for both
// the initial page load and every filter switch; live tick stays a plain
// n=N_LIVE fetch (see liveTick in initRawPage/initInferencePage).
function fetchChartRows(endpoint, pot, filter) {
  return fetchJson(
    endpoint + "?pot=" + pot + "&n=0&range=" + filter + "&max_points=" + KA.CHART_MAX_POINTS
  );
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

function fmtMB(mb) {
  if (mb === null || mb === undefined || isNaN(mb)) return "—";
  var n = Number(mb);
  return n >= 1024 ? (n / 1024).toFixed(2) + " GB" : n.toFixed(1) + " MB";
}

// ── Time range filtering ───────────────────────────────────────────────────
function _filterDataset(values, dates, filter) {
  if (!dates || !dates.length || filter === "overall") {
    return { values: values, dates: dates };
  }

  // Anchor to the latest available reading timestamp
  var lastIso = dates[dates.length - 1];
  var refTime = new Date(lastIso).getTime();
  if (isNaN(refTime)) refTime = Date.now();

  var cutoff = 0;
  if (filter === "5m") {
    cutoff = refTime - (5 * 60 * 1000);
  } else if (filter === "15m") {
    cutoff = refTime - (15 * 60 * 1000);
  } else if (filter === "1h") {
    cutoff = refTime - (60 * 60 * 1000);
  } else if (filter === "today") {
    var todayStart = new Date(refTime);
    todayStart.setHours(0, 0, 0, 0);
    cutoff = todayStart.getTime();
  } else if (filter === "week") {
    cutoff = refTime - (7 * 24 * 60 * 60 * 1000);
  }

  var filtValues = [];
  var filtDates = [];
  for (var i = 0; i < dates.length; i++) {
    var t = new Date(dates[i]).getTime();
    if (!isNaN(t) && t >= cutoff) {
      filtDates.push(dates[i]);
      filtValues.push(values[i]);
    }
  }

  // If filtered slice has fewer than 2 points, return whatever is available
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
    var sdSpan = "";
    if (s.sd_used_mb !== null && s.sd_used_mb !== undefined &&
        s.sd_total_mb !== null && s.sd_total_mb !== undefined) {
      var pctUsed = s.sd_total_mb > 0 ? (s.sd_used_mb / s.sd_total_mb * 100) : 0;
      sdSpan =
        "<span>· 💾 SD: <strong>" + fmtMB(s.sd_used_mb) + " / " + fmtMB(s.sd_total_mb) +
        "</strong> (" + pctUsed.toFixed(0) + "% used)</span>";
    }
    el.innerHTML =
      "<span>📅 Collecting since: <strong>" + fmtTs(s.collecting_since) + "</strong></span>" +
      "<span>· A: <strong>" + s.total_readings_a + "</strong> readings</span>" +
      "<span>· B: <strong>" + s.total_readings_b + "</strong> readings</span>" +
      "<span>· " + ntp + offset + "</span>" +
      "<span>· Last: " + fmtTs(s.last_reading_at) + "</span>" +
      sdSpan;
  }).catch(function () {});
};

// light_lux has no real sensor yet (BH1750 not wired) -- until/unless the
// board starts writing non-flat readings (real or a plausible stand-in;
// see mock-data/synthetic_light_tool locally, gitignored, never committed),
// every value is exactly 0 and the UI shows it as a "pending" placeholder
// rather than a real "0.0 lx" reading. This is data-driven, not a fixed
// on/off switch in the template, so the UI flips automatically the moment
// real-looking data shows up (or reverts automatically if that data is
// ever rolled back) with no redeploy needed either way.
function _isLightPlaceholder(values) {
  if (!values || !values.length) return true;
  return values.every(function (v) { return !v; }); // 0, null, undefined all count
}

// ── raw value card (big number + label) ───────────────────────────────────
function updateRawCard(pot, sensor, meta, rows, isPlaceholder) {
  var el = document.getElementById("raw-" + sensor + "-" + pot);
  if (!el) return;
  var latest = rows.length ? rows[rows.length - 1][sensor] : null;
  if (sensor === "light_lux" && isPlaceholder && latest === 0) latest = null;
  var label = meta.label;
  if (sensor === "light_lux") {
    el.classList.toggle("ka-raw-card--placeholder", !!isPlaceholder);
    label = isPlaceholder ? "Light (pending)" : "Light";
  }
  el.innerHTML =
    "<span class=\"ka-raw-val\">" + fmt(latest, meta.unit) + "</span>" +
    "<span class=\"ka-raw-label\">" + label + "</span>";
}

// ── chart state — per-pot, per-sensor history buffer ──────────────────────
// Keyed by timestamp (not append order): the initial full-history fetch
// (n=0, can take longer than REFRESH_MS against a large JSONL file) and the
// 5s live-tick fetch (last 60 rows) race each other, so whichever resolves
// first cannot be assumed to hold the earlier data. Merging by timestamp
// key makes the buffer correct regardless of arrival order.
var _chartData = {};   // _chartData[pot][sensor] = { byTs: {timestamp: value} }

function _ensureChartData(pot, sensor) {
  if (!_chartData[pot]) _chartData[pot] = {};
  if (!_chartData[pot][sensor]) _chartData[pot][sensor] = { byTs: {} };
  return _chartData[pot][sensor];
}

function _mergeByTimestamp(byTs, rows, valueKey) {
  rows.forEach(function (r) {
    if (r.timestamp == null) return;
    byTs[r.timestamp] = r[valueKey];
  });
}

function _sortedSeries(byTs) {
  var dates = Object.keys(byTs).sort();
  var values = dates.map(function (t) { return byTs[t]; });
  return { values: values, dates: dates };
}

function _appendRows(pot, rows) {
  if (!rows || !rows.length) return;
  var sensors = Object.keys(SENSOR_META);
  sensors.forEach(function (sensor) {
    var buf = _ensureChartData(pot, sensor);
    _mergeByTimestamp(buf.byTs, rows, sensor);
  });
}

// Updates the right-aligned delta badge in a card's title bar, kept
// separate from the chart itself so it doesn't overlap the SVG.
function _updateTrendBadge(id, values, unit) {
  var el = document.getElementById(id);
  if (!el) return;
  var d = KA.trendDelta(values, unit);
  el.textContent = d.text;
  el.className = "ka-card-trend" + (d.cls ? " " + d.cls : "");
}

// Latest reading, always the true most-recent value regardless of the
// active time-range filter — distinct from the trend badge, which is
// computed only over the filtered window.
function _updateCurrentValue(id, value, unit) {
  var el = document.getElementById(id);
  if (!el) return;
  unit = unit || "";
  el.textContent = (value === null || value === undefined || isNaN(value))
    ? ""
    : "Current: " + value.toFixed(3) + unit;
}

// Whether pot's buffered light_lux series (everything seen so far, not just
// the latest fetch batch) still looks like the flat-placeholder state.
function _lightIsPlaceholder(pot) {
  var buf = _chartData[pot] && _chartData[pot]["light_lux"];
  if (!buf) return true;
  return _isLightPlaceholder(_sortedSeries(buf.byTs).values);
}

// Page-level subtitle (not per-pot) — only claims "placeholder" while
// BOTH pots still look flat; if either shows real-looking data, the note
// switches to describing it honestly as a modeled stand-in, not a live
// BH1750 reading (main.py itself is unchanged/still writes the flat 0.0
// placeholder -- any non-flat data here came from a separate, uncommitted
// local tool, not a real sensor).
function _updateLightSensorNote() {
  var el = document.getElementById("ka-light-sensor-note");
  if (!el) return;
  var stillPlaceholder = _lightIsPlaceholder("A") && _lightIsPlaceholder("B");
  el.textContent = stillPlaceholder
    ? "Light sensor: placeholder until BH1750 wired."
    : "Light sensor: not physically wired yet — values shown are a modeled day/night stand-in, not a live reading.";
}

function _renderCharts(pot) {
  var sensors = Object.keys(SENSOR_META);
  var lightPlaceholder = _lightIsPlaceholder(pot);
  _updateLightSensorNote();
  sensors.forEach(function (sensor) {
    var container = document.getElementById("chart-" + sensor + "-" + pot);
    if (!container) return;
    if (sensor === "light_lux") {
      var card = container.closest(".ka-card");
      if (card) {
        card.classList.toggle("ka-card--placeholder", lightPlaceholder);
        var badge = card.querySelector(".ka-placeholder-badge");
        if (badge) badge.style.display = lightPlaceholder ? "" : "none";
      }
    }
    var buf = _ensureChartData(pot, sensor);
    var series = _sortedSeries(buf.byTs);
    if (!series.values.length) return;
    var filt = _filterDataset(series.values, series.dates, KA.currentFilter);
    var chart = KA.trendChart(filt.values, filt.dates, POT_COLOUR[pot], SENSOR_META[sensor].unit, false);
    container.innerHTML = "";
    container.appendChild(chart);
    _updateTrendBadge("trend-" + sensor + "-" + pot, filt.values, SENSOR_META[sensor].unit);
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

  function loadFilterScopedData() {
    pots.forEach(function (pot) {
      fetchChartRows("/api/raw", pot, KA.currentFilter)
        .then(function (rows) {
          if (rows && rows.length) {
            _appendRows(pot, rows);
            var lightPlaceholder = _lightIsPlaceholder(pot);
            sensors.forEach(function (sensor) {
              updateRawCard(pot, sensor, SENSOR_META[sensor], rows, lightPlaceholder);
            });
          }
          _renderCharts(pot);
        })
        .catch(function (e) { console.warn("history fetch error:", e); });
    });
  }

  // Setup time range filter buttons — each switch re-fetches, scoped+decimated
  // to that filter, rather than just re-slicing whatever's already buffered
  // (a sparse "overall" buffer alone isn't dense enough for narrow filters).
  _setupFilterListeners("raw-filter-group", loadFilterScopedData);

  // Load data scoped to the current (default "overall") filter on page load,
  // then refresh status bar.
  KA.refreshStatus();
  loadFilterScopedData();

  function liveTick() {
    KA.refreshStatus();
    pots.forEach(function (pot) {
      fetchJson("/api/raw?pot=" + pot + "&n=" + KA.N_LIVE)
        .then(function (rows) {
          if (!rows || !rows.length) return;

          _appendRows(pot, rows);

          // Always update top value cards with latest reading
          var lightPlaceholder = _lightIsPlaceholder(pot);
          sensors.forEach(function (sensor) {
            updateRawCard(pot, sensor, SENSOR_META[sensor], rows, lightPlaceholder);
          });

          // Re-render chart so the rolling window and axis timestamps advance smoothly
          _renderCharts(pot);
        })
        .catch(function (e) { console.warn("live tick fetch error:", e); });
    });
  }

  setInterval(liveTick, KA.REFRESH_MS);
};

// ── Inference page ─────────────────────────────────────────────────────────
KA.initInferencePage = function (pots) {
  var _infChartData = {}; // _infChartData[pot] = { byTs: {timestamp: rsmi} } — see raw-page note on ordering

  function renderInfChart(pot) {
    var chartContainer = document.getElementById("chart-rsmi-" + pot);
    if (!chartContainer || !_infChartData[pot]) return;
    var series = _sortedSeries(_infChartData[pot].byTs);
    if (!series.values.length) return;
    var filt = _filterDataset(series.values, series.dates, KA.currentFilter);
    var chart = KA.trendChart(filt.values, filt.dates, POT_COLOUR[pot], "", false);
    chartContainer.innerHTML = "";
    chartContainer.appendChild(chart);
    _updateTrendBadge("trend-rsmi-" + pot, filt.values, "");
    _updateCurrentValue("current-rsmi-" + pot, series.values[series.values.length - 1], "");
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

  pots.forEach(function (pot) { _infChartData[pot] = { byTs: {} }; });

  function loadFilterScopedData() {
    pots.forEach(function (pot) {
      fetchChartRows("/api/inference", pot, KA.currentFilter)
        .then(function (rows) {
          if (!rows || !rows.length) return;
          _mergeByTimestamp(_infChartData[pot].byTs, rows, "rsmi");
          renderInfChart(pot);
          updateFlagUI(pot, rows);
        })
        .catch(function (e) { console.warn("inference history fetch error:", e); });
    });
  }

  // Setup time range filter buttons — each switch re-fetches, scoped+decimated
  // to that filter (see initRawPage's identical rationale).
  _setupFilterListeners("inference-filter-group", loadFilterScopedData);

  // Load data scoped to the current (default "overall") filter on page load.
  loadFilterScopedData();

  // Live polling: append new points and advance rolling window
  function liveTick() {
    pots.forEach(function (pot) {
      fetchJson("/api/inference?pot=" + pot + "&n=" + KA.N_LIVE)
        .then(function (rows) {
          if (!rows || !rows.length) return;
          updateFlagUI(pot, rows);
          _mergeByTimestamp(_infChartData[pot].byTs, rows, "rsmi");
          renderInfChart(pot);
        })
        .catch(function (e) { console.warn("inference live tick error:", e); });
    });
  }

  setInterval(liveTick, KA.REFRESH_MS);
};

