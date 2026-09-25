/* AIS Streaming Testing Platform -- presentation layer.
 *
 * Talks only to the FastAPI backend (backend/app/main.py, shapes in
 * backend/app/schemas.py), which serves either the bundled demo dataset or
 * a live MCM_streaming runtime. Never talks to MCM_streaming directly.
 */
(function () {
  "use strict";

  var COLORS = {
    history: "#8ea0c9",
    cv: "#f2994a",
    kalman: "#9b8cf2",
    candidate: "#4fd1c5",
    served: "#f25f5c",
    noModel: "#5a6584",
  };

  var map = L.map("map").setView([59.9, 15.0], 5);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  var vesselLayer = L.layerGroup().addTo(map);
  var detailLayer = L.layerGroup().addTo(map);
  var selectedMmsi = null;
  var firstLoad = true;

  // Record fields can originate from external AIS feeds in bridge mode, so
  // everything interpolated into HTML goes through this.
  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmt(n, digits) {
    if (n === null || n === undefined) return "&ndash;";
    return Number(n).toFixed(digits);
  }

  function apiGet(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
      return r.json();
    });
  }

  function showError(err) {
    console.error(err);
    document.getElementById("metrics-body").innerHTML =
      "<p class='metrics-footnote'>Could not load data: " + esc(err.message) + "</p>";
  }

  function latLngs(anchor, block) {
    var out = [anchor];
    for (var i = 0; i < block.lat.length; i++) out.push([block.lat[i], block.lon[i]]);
    return out;
  }

  function renderMetrics(summary) {
    document.getElementById("data-mode-badge").textContent = summary.data_mode;
    var body = document.getElementById("metrics-body");
    if (!summary.served_vs_baselines.length) {
      body.innerHTML = "<p class='metrics-footnote'>No reconciled ground truth yet for this " +
        "data source. Deltas appear once MCM_streaming's reconcile step has scored forecasts " +
        "(about 10 minutes after they are issued).</p>";
      return;
    }
    var html = "";
    summary.served_vs_baselines.forEach(function (d) {
      var cls = d.mean_delta_m < 0 ? "negative" : "positive";
      html += '<div class="delta-row"><span title="' + esc(d.description) + '">' + esc(d.label) +
        '</span><span class="delta-value ' + cls + '">' + fmt(d.mean_delta_m, 1) + " m</span></div>";
    });
    html += '<p class="metrics-footnote">Mean ADE delta over ' + summary.n_scored +
      " reconciled forecasts; negative = closer to ground truth. " + summary.n_vessels +
      " vessels on the map. Median latency: end-to-end " + fmt(summary.median_e2e_latency_s, 2) +
      " s, model inference " + fmt(summary.median_mcmnet_latency_s, 3) + " s. Model: " +
      esc(summary.model_version || "n/a") + "</p>";
    body.innerHTML = html;
  }

  function renderVesselList(vessels) {
    var ul = document.getElementById("vessel-list");
    ul.innerHTML = "";
    vessels.forEach(function (v) {
      var li = document.createElement("li");
      li.dataset.mmsi = v.mmsi;
      li.innerHTML = '<span class="mmsi">' + esc(v.mmsi) + '</span><span class="meta">' +
        esc(v.ship_class || v.source) + "</span>";
      li.addEventListener("click", function () { selectVessel(v.mmsi, true); });
      ul.appendChild(li);
    });
    markSelected();
  }

  function markSelected() {
    Array.prototype.forEach.call(document.querySelectorAll("#vessel-list li"), function (li) {
      li.classList.toggle("selected", li.dataset.mmsi === String(selectedMmsi));
    });
  }

  function plotVessels(vessels) {
    vesselLayer.clearLayers();
    vessels.forEach(function (v) {
      var color = v.has_model ? COLORS.candidate : COLORS.noModel;
      L.circleMarker([v.last_lat, v.last_lon], {
        radius: 6, color: color, fillColor: color, fillOpacity: 0.9, weight: 2,
      })
        .bindTooltip("MMSI " + esc(v.mmsi) +
          (v.last_sog_kn !== null && v.last_sog_kn !== undefined ? " &middot; " + fmt(v.last_sog_kn, 1) + " kn" : ""))
        .on("click", function () { selectVessel(v.mmsi, true); })
        .addTo(vesselLayer);
    });
  }

  function legendRow(color, text) {
    return '<div class="legend-row"><span class="legend-swatch" style="background:' + color +
      '"></span>' + text + "</div>";
  }

  function renderDetail(d) {
    detailLayer.clearLayers();
    document.getElementById("detail-card").hidden = false;
    var anchor = [d.anchor.lat, d.anchor.lon];

    if (d.history.length > 1) {
      L.polyline(d.history.map(function (p) { return [p.lat, p.lon]; }),
        { color: COLORS.history, weight: 2, dashArray: "3,4" }).addTo(detailLayer);
    }
    if (d.model) {
      d.model.hypotheses.forEach(function (h) {
        L.polyline(latLngs(anchor, h), { color: COLORS.candidate, weight: 1, opacity: 0.25 })
          .addTo(detailLayer);
      });
    }
    L.polyline(latLngs(anchor, d.cv), { color: COLORS.cv, weight: 2 })
      .bindTooltip("Constant Velocity").addTo(detailLayer);
    L.polyline(latLngs(anchor, d.kalman), { color: COLORS.kalman, weight: 2 })
      .bindTooltip("Kalman").addTo(detailLayer);
    var served = d.model && (d.model.routed || d.model.scorer);
    if (served) {
      L.polyline(latLngs(anchor, served), { color: COLORS.served, weight: 3 })
        .bindTooltip("Served rule" + (d.model.routed_mode ? " (" + esc(d.model.routed_mode) + ")" : ""))
        .addTo(detailLayer);
    }
    L.circleMarker(anchor, { radius: 5, color: "#fff", fillColor: "#fff", fillOpacity: 1 })
      .bindTooltip("Anchor (latest position)").addTo(detailLayer);

    document.getElementById("detail-body").innerHTML =
      "<dl>" +
      "<dt>Job ID</dt><dd>" + esc(d.job_id) + "</dd>" +
      "<dt>Source</dt><dd>" + esc(d.source) + "</dd>" +
      "<dt>Model version</dt><dd>" + esc(d.model ? d.model.model_version : "disabled") + "</dd>" +
      (d.model && d.model.routed_mode ? "<dt>Router mode</dt><dd>" + esc(d.model.routed_mode) + "</dd>" : "") +
      "<dt>End-to-end latency</dt><dd>" + fmt(d.e2e_latency_s, 2) + " s</dd>" +
      "<dt>Model inference latency</dt><dd>" +
        fmt(d.model ? d.model.mcmnet_latency_s : null, 3) + " s</dd>" +
      "</dl>" +
      legendRow(COLORS.cv, "Constant Velocity") +
      legendRow(COLORS.kalman, "Kalman") +
      legendRow(COLORS.candidate, "MCM-Net candidates (" + (d.model ? d.model.hypotheses.length : 0) + ")") +
      legendRow(COLORS.served, "Served rule (routed / scored)");
  }

  function selectVessel(mmsi, recenter) {
    selectedMmsi = mmsi;
    markSelected();
    return apiGet("/api/vessels/" + encodeURIComponent(mmsi) + "/prediction").then(function (d) {
      renderDetail(d);
      if (recenter) map.setView([d.anchor.lat, d.anchor.lon], Math.max(map.getZoom(), 11));
    });
  }

  function refresh() {
    return Promise.all([
      apiGet("/api/vessels"),
      apiGet("/api/metrics/summary"),
      apiGet("/api/health"),
    ]).then(function (res) {
      var vessels = res[0];
      renderMetrics(res[1]);
      document.getElementById("source-dir").textContent = res[2].source_dir;
      renderVesselList(vessels);
      plotVessels(vessels);

      if (firstLoad && vessels.length) {
        firstLoad = false;
        map.fitBounds(L.latLngBounds(vessels.map(function (v) { return [v.last_lat, v.last_lon]; }))
          .pad(0.2), { maxZoom: 10 });
      }
      var stillThere = vessels.some(function (v) { return v.mmsi === selectedMmsi; });
      if (stillThere) return selectVessel(selectedMmsi, false);
      if (vessels.length) return selectVessel(vessels[0].mmsi, false);
      selectedMmsi = null;
      detailLayer.clearLayers();
      document.getElementById("detail-card").hidden = true;
    }).catch(showError);
  }

  document.getElementById("reload-btn").addEventListener("click", function () {
    fetch("/api/reload", { method: "POST" }).then(refresh).catch(showError);
  });

  refresh();
})();
