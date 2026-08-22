/*
 * Dependency-free inline-SVG trend chart.
 *
 * Deliberate near-line-for-line JS port of the TrendChart component in
 * ~/workspace/Karbon-africa/distributed-app/web/app/corporate/project-health/page.tsx
 * (lines 249-319) so porting this to real .tsx later is mechanical
 * translation, not a redesign. Same math, same visual language.
 */
window.KA = window.KA || {};

// Shared by callers that render the delta indicator outside the chart
// itself (e.g. in a card title bar) instead of overlapping the SVG.
KA.trendDelta = function (values, unit) {
  unit = unit || "";
  var nums = (values || []).filter(function (v) { return v !== null && v !== undefined && !isNaN(v); });
  if (nums.length < 2) return { text: "", cls: "" };
  var delta = nums[nums.length - 1] - nums[0];
  if (delta < -0.001) return { text: "↓ " + Math.abs(delta).toFixed(3) + unit, cls: "ka-trend-down" };
  if (delta > 0.001) return { text: "↑ " + Math.abs(delta).toFixed(3) + unit, cls: "ka-trend-up" };
  return { text: "stable", cls: "ka-trend-stable" };
};

KA.trendChart = function (values, dates, colour, unit, stressed) {
  unit = unit || "";
  // Keep value/date pairs aligned by filtering them together — filtering
  // nums and dates independently (the old behaviour) silently breaks index
  // alignment between the two arrays the moment either contains a gap, which
  // the per-point hover tooltip below depends on being correct.
  var pairs = [];
  for (var vi = 0; vi < values.length; vi++) {
    var v = values[vi];
    if (v !== null && v !== undefined && !isNaN(v)) {
      pairs.push({ v: v, d: (dates && dates[vi] != null) ? dates[vi] : null });
    }
  }
  var nums = pairs.map(function (p) { return p.v; });
  var numDates = pairs.map(function (p) { return p.d; });
  if (nums.length < 2) {
    var empty = document.createElement("div");
    empty.className = "ka-trend-empty";
    empty.textContent = "No trend data";
    return empty;
  }

  var minVal = Math.min.apply(null, nums);
  var maxVal = Math.max.apply(null, nums);
  var range = (maxVal - minVal) || 0.001;
  var W = 240, H = 52, px = 6, py = 6;
  var toX = function (i) { return px + (i / (nums.length - 1)) * (W - 2 * px); };
  var toY = function (v) { return H - py - ((v - minVal) / range) * (H - 2 * py); };
  var pts = nums.map(function (v, i) { return { x: toX(i), y: toY(v) }; });
  var linePts = pts.map(function (p) { return p.x.toFixed(1) + "," + p.y.toFixed(1); }).join(" ");
  var fillPts = pts[0].x.toFixed(1) + "," + H + " " + linePts + " " + pts[pts.length - 1].x.toFixed(1) + "," + H;

  var peakIdx = nums.indexOf(maxVal);
  var hasMidPeak = peakIdx > 0 && peakIdx < nums.length - 1;

  var validDates = numDates.filter(function (d) { return d != null; });
  var firstDate = validDates[0];
  var lastDate = validDates[validDates.length - 1];

  var wrap = document.createElement("div");

  var box = document.createElement("div");
  box.className = "ka-trend-box" + (stressed ? " ka-trend-stressed" : "");

  var svgNs = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(svgNs, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "ka-trend-svg");

  var polygon = document.createElementNS(svgNs, "polygon");
  polygon.setAttribute("fill", colour);
  polygon.setAttribute("fill-opacity", "0.08");
  polygon.setAttribute("points", fillPts);
  svg.appendChild(polygon);

  var polyline = document.createElementNS(svgNs, "polyline");
  polyline.setAttribute("fill", "none");
  polyline.setAttribute("stroke", colour);
  polyline.setAttribute("stroke-width", "1");
  polyline.setAttribute("stroke-linejoin", "round");
  polyline.setAttribute("stroke-linecap", "round");
  polyline.setAttribute("points", linePts);
  svg.appendChild(polyline);

  if (hasMidPeak) {
    var peakDot = document.createElementNS(svgNs, "circle");
    peakDot.setAttribute("cx", pts[peakIdx].x);
    peakDot.setAttribute("cy", pts[peakIdx].y);
    peakDot.setAttribute("r", "2");
    peakDot.setAttribute("fill", colour);
    peakDot.setAttribute("fill-opacity", "0.4");
    peakDot.setAttribute("stroke", colour);
    peakDot.setAttribute("stroke-width", "0.8");
    svg.appendChild(peakDot);
  }

  var lastDot = document.createElementNS(svgNs, "circle");
  lastDot.setAttribute("cx", pts[pts.length - 1].x);
  lastDot.setAttribute("cy", pts[pts.length - 1].y);
  lastDot.setAttribute("r", "2.5");
  lastDot.setAttribute("fill", colour);
  svg.appendChild(lastDot);

  // ── Hover tooltip: value + timestamp + point-to-point trend arrow ───────
  var hoverDot = document.createElementNS(svgNs, "circle");
  hoverDot.setAttribute("r", "3");
  hoverDot.setAttribute("fill", colour);
  hoverDot.setAttribute("stroke", "#fff");
  hoverDot.setAttribute("stroke-width", "1");
  hoverDot.setAttribute("class", "ka-trend-hoverdot");
  svg.appendChild(hoverDot);

  var tooltip = document.createElement("div");
  tooltip.className = "ka-trend-tooltip";

  // Same up/down/stable thresholds+classes as KA.trendDelta, computed
  // point-to-point instead of start-to-end.
  function _pointArrow(idx) {
    if (idx <= 0) return { text: "—", cls: "ka-trend-stable" };
    var delta = nums[idx] - nums[idx - 1];
    if (delta < -0.001) return { text: "↓", cls: "ka-trend-down" };
    if (delta > 0.001) return { text: "↑", cls: "ka-trend-up" };
    return { text: "→", cls: "ka-trend-stable" };
  }

  function _nearestIndex(clientX) {
    var rect = svg.getBoundingClientRect();
    var ratio = rect.width ? (clientX - rect.left) / rect.width : 0;
    ratio = Math.max(0, Math.min(1, ratio));
    return Math.round(ratio * (nums.length - 1));
  }

  function _showTooltip(clientX) {
    var idx = _nearestIndex(clientX);
    var p = pts[idx];
    hoverDot.setAttribute("cx", p.x);
    hoverDot.setAttribute("cy", p.y);
    hoverDot.setAttribute("visibility", "visible");

    var arrow = _pointArrow(idx);
    tooltip.innerHTML = "";
    var tsSpan = document.createElement("span");
    tsSpan.className = "ka-trend-tooltip-ts";
    tsSpan.textContent = numDates[idx] ? KA.fmtTimeOrDate(numDates[idx], spanMs) : "";
    var valSpan = document.createElement("span");
    valSpan.className = "ka-trend-tooltip-val";
    valSpan.textContent = nums[idx].toFixed(3) + unit;
    var arrowSpan = document.createElement("span");
    arrowSpan.className = "ka-trend-tooltip-arrow " + arrow.cls;
    arrowSpan.textContent = arrow.text;
    tooltip.appendChild(tsSpan);
    tooltip.appendChild(valSpan);
    tooltip.appendChild(arrowSpan);

    // Anchor horizontally to the point's position (fraction of the SVG's
    // viewBox width), clamped so the tooltip doesn't overflow the box.
    var leftPct = (p.x / W) * 100;
    leftPct = Math.max(8, Math.min(92, leftPct));
    tooltip.style.left = leftPct + "%";
    tooltip.style.visibility = "visible";
  }

  function _hideTooltip() {
    hoverDot.setAttribute("visibility", "hidden");
    tooltip.style.visibility = "hidden";
  }

  hoverDot.setAttribute("visibility", "hidden");
  tooltip.style.visibility = "hidden";

  svg.addEventListener("mousemove", function (e) { _showTooltip(e.clientX); });
  svg.addEventListener("mouseleave", _hideTooltip);
  svg.addEventListener("touchmove", function (e) {
    if (e.touches && e.touches.length) {
      _showTooltip(e.touches[0].clientX);
      e.preventDefault();
    }
  }, { passive: false });
  svg.addEventListener("touchend", _hideTooltip);

  box.appendChild(svg);
  box.appendChild(tooltip);
  wrap.appendChild(box);

  var spanMs = (firstDate && lastDate) ? (new Date(lastDate).getTime() - new Date(firstDate).getTime()) : 0;
  var dateRow = KA.rowOf(
    firstDate ? KA.fmtTimeOrDate(firstDate, spanMs) : "",
    "trend",
    lastDate ? KA.fmtTimeOrDate(lastDate, spanMs) : ""
  );
  dateRow.className = "ka-trend-dates";
  wrap.appendChild(dateRow);

  var rangeRow = KA.rowOf(
    "low " + minVal.toFixed(2) + unit,
    "high " + maxVal.toFixed(2) + unit
  );
  rangeRow.className = "ka-trend-range";
  wrap.appendChild(rangeRow);

  return wrap;
};

KA.rowOf = function () {
  var row = document.createElement("div");
  for (var i = 0; i < arguments.length; i++) {
    var span = document.createElement("span");
    span.textContent = arguments[i];
    row.appendChild(span);
  }
  return row;
};

KA.fmtTimeOrDate = function (iso, spanMs) {
  var d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  // If span is within 24 hours, show readable time (HH:MM:SS if under 15m, HH:MM otherwise)
  if (spanMs && spanMs < 15 * 60 * 1000) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  if (!spanMs || spanMs < 24 * 60 * 60 * 1000) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  // If spanning multiple days, show Month Day + HH:MM
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

KA.fmtShortDate = function (iso) {
  var d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

