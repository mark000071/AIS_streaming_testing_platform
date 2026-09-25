/* AIS Streaming Testing Platform -- presentation layer.
 *
 * Talks to the FastAPI backend in backend/app/main.py, which serves either
 * the bundled demo dataset or a live MCM_streaming serving feed (see
 * docs/ARCHITECTURE.md). This file only knows the small JSON API in
 * backend/app/schemas.py -- it never talks to MCM_streaming directly.
 */
(function () {
  "use strict";

  var map = L.map("map", { zoomControl: true }).setView([59.9, 15.0], 5);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  var layers = {
    vessels: L.layerGroup().addTo(map),
    history: L.layerGroup().addTo(map),
    cv: L.layerGroup().addTo(map),
    kalman: L.layerGroup().addTo(map),
    hypotheses: L.layerGroup().addTo(map),
    served: L.layerGroup().addTo(map),
  };

  var selectedMmsi = null;

  function apiGet(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error(path + " -> " + r.status);
      return r.json();
    });
  }

  function latLngPairs(block) {
    var out = [];
    for (var i = 0; i < block.lat.length; i++) out.push([block.lat[i], block.lon[i]]);
    return out;
  }

  function fmt(n, digits) {
    if (n === null || n === undefined) return "&ndash;";
    return Number(n).toFixed(digits === undefined ? 1 : digits);
  }

  function renderMetrics(summary) {
    document.getElementById("data-mode-badge").textContent = summary.data_mode;
    var body = document.getElementById("metrics-body");
    if (!summary.served_vs_baselines.length) {
      body.innerHTML = "<p class='metrics-footnote'>No reconciled ground truth available yet " +
        "for this data source -- deltas appear once MCM_streaming's reconcile step has scored " +
        "predictions (see docs/DATA_CONTRACT.md).</p>";
      return;
    }
    var html = "";
    summary.served_vs_baselines.forEach(function (d) {
      var cls = d.mean_delta_m < 0 ? "negative" : "positive";
      html += '<div class="delta-row"><span title="' + d.description + '">' + d.label +
        '</span><span class="delta-value ' + cls + '">' + fmt(d.mean_delta_m, 2) + ' m (n=' + d.n + ')</span></div>';
    });
    html += '<p class="metrics-footnote">Negative = served rule closer to ground truth on average. ' +
      'n=' + summary.n_predictions + ' predictions across ' + summary.n_vessels + ' vessels. ' +
      'Median e2e latency ' + fmt(summary.median_e2e_latency_s, 2) + 's, model inference ' +
      fmt(summary.median_mcmnet_latency_s, 3) + 's. Model: ' + (summary.model_version || "n/a") + '</p>';
    body.innerHTML = html;
  }

  function renderVesselList(vessels) {
    var ul = document.getElementById("vessel-list");
    ul.innerHTML = "";
    vessels.forEach(function (v) {
      var li = document.createElement("li");
      li.innerHTML = '<span class="mmsi">' + v.mmsi + '</span>' +
        '<span class="meta">' + (v.ship_class || v.source) + '</span>';
      li.addEventListener("click", function () { selectVessel(v.mmsi); });
      if (v.mmsi === selectedMmsi) li.className = "selected";
      ul.appendChild(li);
    });
  }

  function plotVessels(vessels) {
    layers.vessels.clearLayers();
    vessels.forEach(function (v) {
      var marker = L.circleMarker([v.last_lat, v.last_lon], {
        radius: 6,
        color: v.has_prediction ? "#4fd1c5" : "#5a6584",
        fillColor: v.has_prediction ? "#4fd1c5" : "#5a6584",
        fillOpacity: 0.9,
        weight: 2,
      }).addTo(layers.vessels);
      marker.bindTooltip("MMSI " + v.mmsi + (v.last_sog_kn ? " &middot; " + fmt(v.last_sog_kn, 1) + " kn" : ""));
      marker.on("click", function () { selectVessel(v.mmsi); });
    });
  }

  function clearDetailLayers() {
    [layers.history, layers.cv, layers.kalman, layers.hypotheses, layers.served].forEach(function (l) {
      l.clearLayers();
    });
  }

  function renderDetail(detail) {
    clearDetailLayers();
    document.getElementById("detail-card").hidden = false;

    var histPts = detail.history.map(function (p) { return [p.lat, p.lon]; });
    if (histPts.length > 1) {
      L.polyline(histPts, { color: "#8ea0c9", weight: 2, dashArray: "3,4" }).addTo(layers.history);
    }
    var anchor = [detail.anchor.lat, detail.anchor.lon];
    L.circleMarker(anchor, { radius: 5, color: "#fff", fillColor: "#fff", fillOpacity: 1 })
      .addTo(layers.history).bindTooltip("Anchor (now)");

    if (detail.model) {
      detail.model.hypotheses.forEach(function (h, idx) {
        var pts = [anchor].concat(latLngPairs(h));
        L.polyline(pts, { color: "#4fd1c5", weight: 1, opacity: 0.18 }).addTo(layers.hypotheses);
      });
    }

    L.polyline([anchor].concat(latLngPairs(detail.cv)),
      { color: "#f2994a", weight: 2, opacity: 0.9 }).addTo(layers.cv).bindTooltip("Constant Velocity");
    L.polyline([anchor].concat(latLngPairs(detail.kalman)),
      { color: "#9b8cf2", weight: 2, opacity: 0.9 }).addTo(layers.kalman).bindTooltip("Kalman");

    var servedBlock = detail.model && (detail.model.routed || detail.model.scorer);
    if (servedBlock) {
      L.polyline([anchor].concat(latLngPairs(servedBlock)),
        { color: "#f25f5c", weight: 3, opacity: 0.95 }).addTo(layers.served)
        .bindTooltip("Served rule" + (detail.model.routed_mode ? " (" + detail.model.routed_mode + ")" : ""));
    }

    var dl = document.getElementById("detail-body");
    dl.innerHTML =
      "<dl>" +
      "<dt>Job ID</dt><dd>" + detail.job_id + "</dd>" +
      "<dt>Source</dt><dd>" + detail.source + "</dd>" +
      "<dt>Model version</dt><dd>" + (detail.model ? detail.model.model_version : "disabled") + "</dd>" +
      "<dt>End-to-end latency</dt><dd>" + fmt(detail.e2e_latency_s, 2) + " s</dd>" +
      "<dt>Model inference latency</dt><dd>" + fmt(detail.model && detail.model.mcmnet_latency_s, 3) + " s</dd>" +
      "</dl>" +
      '<div class="legend-row"><span class="legend-swatch" style="background:#f2994a"></span>Constant Velocity</div>' +
      '<div class="legend-row"><span class="legend-swatch" style="background:#9b8cf2"></span>Kalman</div>' +
      '<div class="legend-row"><span class="legend-swatch" style="background:#4fd1c5"></span>MCM-Net candidates (' +
        (detail.model ? detail.model.hypotheses.length : 0) + ')</div>' +
      '<div class="legend-row"><span class="legend-swatch" style="background:#f25f5c"></span>Served rule (routed/scored)</div>';
  }

  function selectVessel(mmsi) {
    selectedMmsi = mmsi;
    Array.prototype.forEach.call(document.querySelectorAll("#vessel-list li"), function (li) {
      li.className = li.querySelector(".mmsi").textContent == String(mmsi) ? "selected" : "";
    });
    apiGet("/api/vessels/" + mmsi + "/prediction").then(function (detail) {
      renderDetail(detail);
      map.setView([detail.anchor.lat, detail.anchor.lon], Math.max(map.getZoom(), 9));
    }).catch(function (err) { console.error(err); });
  }

  function refresh() {
    apiGet("/api/vessels").then(function (vessels) {
      renderVesselList(vessels);
      plotVessels(vessels);
      if (vessels.length && selectedMmsi === null) {
        selectVessel(vessels[0].mmsi);
      }
      if (vessels.length) {
        var group = L.featureGroup(vessels.map(function (v) { return L.marker([v.last_lat, v.last_lon]); }));
        map.fitBounds(group.getBounds().pad(0.4));
      }
    });
    apiGet("/api/metrics/summary").then(renderMetrics);
    apiGet("/api/health").then(function (h) {
      document.getElementById("source-dir").textContent = h.source_dir;
    });
  }

  document.getElementById("reload-btn").addEventListener("click", function () {
    fetch("/api/reload", { method: "POST" }).then(refresh);
  });

  refresh();
})();
