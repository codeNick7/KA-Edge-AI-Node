/*
 * Dependency-free inline-SVG trend chart.
 *
 * Deliberate near-line-for-line JS port of the TrendChart component in
 * ~/workspace/Karbon-africa/distributed-app/web/app/corporate/project-health/page.tsx
 * (lines 249-319) so porting this to real .tsx later is mechanical
 * translation, not a redesign. Same math, same visual language.
 */
window.KA = window.KA || {};

KA.trendChart = function (values, dates, colour, unit, stressed) {
  unit = unit || "";
  var nums = values.filter(function (v) { return v !== null && v !== undefined && !isNaN(v); });
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
  var delta = nums[nums.length - 1] - nums[0];
  var trendUp = delta > 0.001;
  var trendDown = delta < -0.001;

  var validDates = (dates || []).filter(function (d) { return d != null; });
  var firstDate = validDates[0];
  var lastDate = validDates[validDates.length - 1];

  var wrap = document.createElement("div");

  var box = document.createElement("div");
  box.className = "ka-trend-box" + (stressed ? " ka-trend-stressed" : "");

  var deltaEl = document.createElement("div");
  deltaEl.className = "ka-trend-delta";
  var deltaSpan = document.createElement("span");
  if (trendDown) {
    deltaSpan.className = "ka-trend-down";
    deltaSpan.textContent = "↓ " + Math.abs(delta).toFixed(3) + unit;
  } else if (trendUp) {
    deltaSpan.className = "ka-trend-up";
    deltaSpan.textContent = "↑ " + Math.abs(delta).toFixed(3) + unit;
  } else {
    deltaSpan.className = "ka-trend-stable";
    deltaSpan.textContent = "stable";
  }
  deltaEl.appendChild(deltaSpan);
  box.appendChild(deltaEl);

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

  box.appendChild(svg);
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

