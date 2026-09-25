import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type StyleSpecification } from "maplibre-gl";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { IconLayer, PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import type { Layer, PickingInfo } from "@deck.gl/core";
import {
  fmtM,
  fmtTime,
  getJSON,
  type Frame,
  type LonLat,
  type ScoreMsg,
  type VesselDetail,
  type VesselRow,
  type WindowDetail,
} from "../api";
import { hexToRgb, ink, predictorColor } from "../colors";

const STYLE = { light: "https://tiles.openfreemap.org/styles/positron", dark: "https://tiles.openfreemap.org/styles/dark" };
const SEAMARK = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png";
const HORIZON_S = 600;
const TRUTH_GRACE_S = 60;
const MIN_TRUTH_COVERAGE = 0.5;
const KN_TO_MS = 0.514444;
const HEADING_LOOKAHEAD_S = 300; // COG/SOG arrow length for ships without a forecast
const MIN_MOVING_KN = 0.5;

interface Verdict {
  window_id: string;
  mmsi: number;
  anchor_ts: number;
  scores: Record<string, ScoreMsg>;
}

const fallbackStyle = (dark: boolean): StyleSpecification => ({
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": dark ? "#1a1a19" : "#eef0f1" } }],
});

async function resolveStyle(dark: boolean): Promise<string | StyleSpecification> {
  const url = dark ? STYLE.dark : STYLE.light;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(url, { signal: ctrl.signal });
    clearTimeout(t);
    if (r.ok) return url;
  } catch {
    /* offline or blocked basemap: fall back to a plain background */
  }
  return fallbackStyle(dark);
}

function addSeamarks(map: maplibregl.Map, visible: boolean) {
  if (map.getSource("seamark")) return;
  map.addSource("seamark", { type: "raster", tiles: [SEAMARK], tileSize: 256, attribution: "© OpenSeaMap contributors" });
  map.addLayer({ id: "seamark", type: "raster", source: "seamark", layout: { visibility: visible ? "visible" : "none" } });
}

const valid = (p: (LonLat | [null, null])[]): LonLat[] => p.filter((q): q is LonLat => q[0] != null && q[1] != null);

/** Bearing in degrees clockwise from north, on a local equirectangular approximation. */
function bearing(a: LonLat, b: LonLat): number {
  const k = Math.cos((a[1] * Math.PI) / 180);
  return (Math.atan2((b[0] - a[0]) * k, b[1] - a[1]) * 180) / Math.PI;
}

/** Point `dist_m` metres from `p` along `bearingDeg`. */
function project(p: LonLat, bearingDeg: number, dist_m: number): LonLat {
  const r = (bearingDeg * Math.PI) / 180;
  const dLat = (dist_m * Math.cos(r)) / 111_320;
  const dLon = (dist_m * Math.sin(r)) / (111_320 * Math.cos((p[1] * Math.PI) / 180));
  return [p[0] + dLon, p[1] + dLat];
}

interface Arrow {
  at: LonLat;
  bearing: number;
  color: number[];
}

/** Arrowhead at the end of a path, oriented along its last non-degenerate segment. */
function arrowAtEnd(path: LonLat[], color: number[]): Arrow | null {
  const end = path[path.length - 1];
  for (let i = path.length - 2; i >= 0; i--) {
    const p = path[i];
    if (Math.abs(p[0] - end[0]) + Math.abs(p[1] - end[1]) > 1e-7) return { at: end, bearing: bearing(p, end), color };
  }
  return null;
}

let arrowUrl: string | null = null;
/** Arrowhead icon (pointing up = north), drawn once and handed to deck.gl as a PNG data URL. */
function arrowAtlas(): string {
  if (arrowUrl) return arrowUrl;
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  if (g) {
    g.fillStyle = "#fff";
    g.beginPath();
    g.moveTo(32, 2);
    g.lineTo(60, 60);
    g.lineTo(32, 44);
    g.lineTo(4, 60);
    g.closePath();
    g.fill();
  }
  arrowUrl = c.toDataURL("image/png");
  return arrowUrl;
}
const ARROW_MAPPING = { arrow: { x: 0, y: 0, width: 64, height: 64, anchorY: 32, mask: true } };

export default function MapPage({ dark }: { dark: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const overlay = useRef<MapboxOverlay | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const vessels = useRef(new Map<number, VesselRow>());
  const [ver, setVer] = useState(0);
  const [clock, setClock] = useState<Record<string, number>>({});
  const [stats, setStats] = useState<Record<string, number>>({});
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const selectedRef = useRef<number | null>(null);
  const [detail, setDetail] = useState<VesselDetail | null>(null);
  const [windowId, setWindowId] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  // follow the vessel's newest forecast unless the user picked a specific window
  const pinned = useRef(false);
  // re-fit the map only after a user action, not on every auto-advance to a new window
  const fitNext = useRef(false);
  const [showAllK, setShowAllK] = useState(true);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [verdicts, setVerdicts] = useState<Verdict[]>([]);
  const [seamarks, setSeamarks] = useState(true);
  const seamarksRef = useRef(seamarks);
  seamarksRef.current = seamarks;
  selectedRef.current = selected;
  const t = ink(dark);

  // ---- map + overlay ----------------------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    resolveStyle(dark).then((style) => {
      if (cancelled || !container.current) return;
      if (!mapRef.current) {
        const map = new maplibregl.Map({
          container: container.current,
          style,
          center: [23.6, 59.9],
          zoom: 6.2,
          attributionControl: { compact: true, customAttribution: "AIS © Fintraffic / Digitraffic, CC BY 4.0" },
        });
        map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
        const ov = new MapboxOverlay({ interleaved: false, layers: [] });
        map.addControl(ov);
        map.on("style.load", () => addSeamarks(map, seamarksRef.current));
        mapRef.current = map;
        overlay.current = ov;
        setMapReady(true);
      } else {
        mapRef.current.setStyle(style);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [dark]);

  useEffect(
    () => () => {
      mapRef.current?.remove();
      mapRef.current = null;
    },
    [],
  );

  useEffect(() => {
    const map = mapRef.current;
    if (map?.getLayer("seamark")) map.setLayoutProperty("seamark", "visibility", seamarks ? "visible" : "none");
  }, [seamarks]);

  // ---- live positions (SSE) -----------------------------------------------------------------------------------
  useEffect(() => {
    const es = new EventSource("/stream/positions");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("frame", (ev) => {
      const f = JSON.parse((ev as MessageEvent).data) as Frame;
      const vs = vessels.current;
      if (f.full) vs.clear();
      for (const row of f.vessels) vs.set(row[0], row);
      for (const m of f.removed) vs.delete(m);
      setClock(f.clock);
      setStats(f.stats);
      setVer((v) => v + 1);
      const sel = selectedRef.current;
      if (sel != null && f.events.some((e) => e.mmsi === sel)) setRefresh((r) => r + 1);
    });
    return () => es.close();
  }, []);

  // ---- verdicts (SSE) ---------------------------------------------------------------------------------------------
  useEffect(() => {
    const es = new EventSource("/stream/scores");
    es.addEventListener("score", (ev) => {
      const s = JSON.parse((ev as MessageEvent).data) as ScoreMsg;
      setVerdicts((prev) => {
        const i = prev.findIndex((v) => v.window_id === s.window_id);
        if (i >= 0) {
          const next = prev.slice();
          next[i] = { ...next[i], scores: { ...next[i].scores, [s.predictor_id]: s } };
          return next;
        }
        return [{ window_id: s.window_id, mmsi: s.mmsi, anchor_ts: s.anchor_ts, scores: { [s.predictor_id]: s } }, ...prev].slice(
          0,
          40,
        );
      });
    });
    return () => es.close();
  }, []);

  // ---- selected vessel detail -----------------------------------------------------------------------------------
  useEffect(() => {
    if (selected == null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    getJSON<VesselDetail>(`/v1/vessels/${selected}/latest`)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        // the newest window only counts as the ship's current forecast if its anchor is recent; after a
        // reporting gap (or a replay loop restart) it can be far older than the ship's position
        const latest = d.windows[0];
        const current = latest && (!d.vessel || d.vessel.ts - latest.anchor_ts <= HORIZON_S) ? latest : null;
        setWindowId((cur) => {
          if (pinned.current && cur && d.windows.some((w) => w.window_id === cur)) return cur;
          return current?.window_id ?? null;
        });
      })
      .catch(() => !cancelled && setDetail(null));
    return () => {
      cancelled = true;
    };
  }, [selected, refresh]);

  const selectVessel = (mmsi: number) => {
    pinned.current = false;
    fitNext.current = true;
    setSelected(mmsi);
  };

  const pickWindow = (wid: string) => {
    pinned.current = true;
    fitNext.current = true;
    setWindowId(wid);
  };

  const openWindow = async (wid: string, mmsi: number) => {
    pinned.current = true;
    fitNext.current = true;
    setSelected(mmsi);
    setWindowId(wid);
    try {
      const w = await getJSON<WindowDetail>(`/v1/windows/${encodeURIComponent(wid)}`);
      setDetail((d) => {
        const base = d && d.vessel?.mmsi === mmsi ? d : { vessel: null, windows: [] };
        return { ...base, windows: [w, ...base.windows.filter((x) => x.window_id !== wid)] };
      });
    } catch {
      /* window aged out of both live state and archive */
    }
  };

  const win = detail?.windows.find((w) => w.window_id === windowId) ?? null;
  const predictorIds = useMemo(() => {
    const ids = new Set<string>();
    if (win) Object.keys(win.candidates).forEach((k) => ids.add(k));
    verdicts.forEach((v) => Object.keys(v.scores).forEach((k) => ids.add(k)));
    return [...ids].sort();
  }, [win, verdicts]);

  // fit the selection into view after a user action (vessel click, window chip, verdict row)
  const track = useMemo<LonLat[][]>(
    () => (detail && (detail.vessel?.mmsi ?? detail.windows[0]?.mmsi) === selected ? (detail.track ?? []) : []),
    [detail, selected],
  );
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !fitNext.current || selected == null) return;
    const mine = detail && (detail.vessel?.mmsi ?? detail.windows[0]?.mmsi) === selected;
    if (!mine) return;
    const pts: LonLat[] = track.flat();
    if (win) {
      pts.push(...win.history, ...(win.truth ? valid(win.truth.path) : []));
      Object.values(win.candidates).forEach((c) => c.paths.forEach((p) => pts.push(...valid(p))));
    }
    const v = detail?.vessel;
    if (v) {
      pts.push([v.lon, v.lat]);
      if (!win && v.sog != null && v.cog != null && v.sog >= MIN_MOVING_KN)
        pts.push(project([v.lon, v.lat], v.cog, v.sog * KN_TO_MS * HEADING_LOOKAHEAD_S));
    }
    if (!pts.length) return;
    fitNext.current = false;
    const b = new maplibregl.LngLatBounds(pts[0], pts[0]);
    pts.forEach((p) => b.extend(p));
    const wide = window.innerWidth > 760;
    map.fitBounds(b, { padding: { top: 60, bottom: wide ? 60 : 280, left: wide ? 420 : 40, right: 60 }, maxZoom: 13, duration: 700 });
  }, [win, detail, selected, track, mapReady]);

  // ---- deck.gl layers ------------------------------------------------------------------------------------------------
  useEffect(() => {
    const ov = overlay.current;
    if (!ov) return;
    const rows = [...vessels.current.values()];
    const primary = hexToRgb(t.primary);
    const secondary = hexToRgb(t.secondary);
    const muted = hexToRgb(t.muted);
    const surface = hexToRgb(t.surface);
    const layers: Layer[] = [
      new ScatterplotLayer<VesselRow>({
        id: "vessels",
        data: rows,
        getPosition: (d) => [d[1], d[2]],
        getRadius: (d) => (d[0] === selected ? 7 : d[6] ? 3.2 : 2.4),
        radiusUnits: "pixels",
        stroked: true,
        lineWidthUnits: "pixels",
        getLineWidth: (d) => (d[0] === selected ? 2 : 0),
        getLineColor: [...surface, 255],
        getFillColor: (d) => (d[0] === selected ? [...primary, 255] : d[6] ? [...secondary, 220] : [...muted, 150]),
        pickable: true,
        onClick: (info: PickingInfo<VesselRow>) => info.object && selectVessel(info.object[0]),
        updateTriggers: { getFillColor: [selected, dark], getRadius: [selected], getLineWidth: [selected] },
      }),
    ];
    const arrows: Arrow[] = [];
    const segments = track.filter((seg) => seg.length > 1);
    if (segments.length) {
      layers.push(
        new PathLayer({
          id: "track",
          data: segments.map((path) => ({ path })),
          getPath: (d: { path: LonLat[] }) => d.path,
          getColor: [...muted, 170],
          getWidth: 1.5,
          widthUnits: "pixels",
          capRounded: true,
          jointRounded: true,
        }),
      );
    }
    const selRow = selected != null ? vessels.current.get(selected) : undefined;
    if (!win && selRow && selRow[3] != null && selRow[4] != null && selRow[3] >= MIN_MOVING_KN) {
      const pos: LonLat = [selRow[1], selRow[2]];
      const ahead = project(pos, selRow[4], selRow[3] * KN_TO_MS * HEADING_LOOKAHEAD_S);
      layers.push(
        new PathLayer({
          id: "heading",
          data: [{ path: [pos, ahead] }],
          getPath: (d: { path: LonLat[] }) => d.path,
          getColor: [...primary, 200],
          getWidth: 2,
          widthUnits: "pixels",
          capRounded: true,
        }),
      );
      arrows.push({ at: ahead, bearing: selRow[4], color: [...primary, 230] });
    }
    if (win) {
      layers.push(
        new PathLayer({
          id: "history",
          data: [{ path: win.history }],
          getPath: (d: { path: LonLat[] }) => d.path,
          getColor: [...muted, 255],
          getWidth: 2,
          widthUnits: "pixels",
          capRounded: true,
          jointRounded: true,
        }),
      );
      const cand: { path: LonLat[]; color: number[]; width: number; end: LonLat }[] = [];
      for (const [pid, c] of Object.entries(win.candidates)) {
        if (hidden.has(pid)) continue;
        const rgb = hexToRgb(predictorColor(pid, dark));
        c.paths.forEach((p, k) => {
          const isSel = k === c.selected;
          if (!isSel && !showAllK) return;
          const path = [win.anchor, ...valid(p)];
          cand.push({ path, color: [...rgb, isSel ? 255 : 110], width: isSel ? 2.5 : 1.2, end: path[path.length - 1] });
        });
      }
      layers.push(
        new PathLayer({
          id: "candidates",
          data: cand,
          getPath: (d: (typeof cand)[number]) => d.path,
          getColor: (d: (typeof cand)[number]) => d.color as [number, number, number, number],
          getWidth: (d: (typeof cand)[number]) => d.width,
          widthUnits: "pixels",
          capRounded: true,
          jointRounded: true,
        }),
        new ScatterplotLayer({
          id: "candidate-ends",
          data: cand.filter((c) => c.width > 2),
          getPosition: (d: (typeof cand)[number]) => d.end,
          getFillColor: (d: (typeof cand)[number]) => d.color as [number, number, number, number],
          getRadius: 4,
          radiusUnits: "pixels",
          stroked: true,
          getLineColor: [...surface, 255],
          getLineWidth: 2,
          lineWidthUnits: "pixels",
        }),
      );
      cand.filter((c) => c.width > 2).forEach((c) => {
        const a = arrowAtEnd(c.path, c.color);
        if (a) arrows.push(a);
      });
      if (win.truth) {
        const tp = [win.anchor, ...valid(win.truth.path)];
        layers.push(
          new PathLayer({
            id: "truth",
            data: [{ path: tp }],
            getPath: (d: { path: LonLat[] }) => d.path,
            getColor: [...primary, 255],
            getWidth: 3,
            widthUnits: "pixels",
            capRounded: true,
            jointRounded: true,
          }),
          new ScatterplotLayer({
            id: "truth-end",
            data: [tp[tp.length - 1]],
            getPosition: (d: LonLat) => d,
            getFillColor: [...primary, 255],
            getRadius: 4.5,
            radiusUnits: "pixels",
            stroked: true,
            getLineColor: [...surface, 255],
            getLineWidth: 2,
            lineWidthUnits: "pixels",
          }),
        );
      }
      layers.push(
        new ScatterplotLayer({
          id: "anchor",
          data: [win.anchor],
          getPosition: (d: LonLat) => d,
          getFillColor: [...surface, 255],
          getLineColor: [...primary, 255],
          stroked: true,
          getRadius: 5,
          radiusUnits: "pixels",
          getLineWidth: 2,
          lineWidthUnits: "pixels",
        }),
      );
    }
    if (arrows.length) {
      layers.push(
        new IconLayer<Arrow>({
          id: "arrows",
          data: arrows,
          iconAtlas: arrowAtlas(),
          iconMapping: ARROW_MAPPING,
          getIcon: () => "arrow",
          getPosition: (d) => d.at,
          getAngle: (d) => -d.bearing,
          getColor: (d) => d.color as [number, number, number, number],
          getSize: 14,
          sizeUnits: "pixels",
          billboard: false,
        }),
      );
    }
    ov.setProps({
      layers,
      getTooltip: ({ object, layer }: PickingInfo) =>
        layer?.id === "vessels" && object
          ? {
              text: `MMSI ${(object as VesselRow)[0]}\n${(object as VesselRow)[3] ?? "–"} kn · ${(object as VesselRow)[4] ?? "–"}°`,
              style: { background: t.surface, color: t.primary, fontSize: "12px", borderRadius: "6px", padding: "6px 8px" },
            }
          : null,
    });
  }, [ver, win, track, selected, showAllK, hidden, dark, t.primary, t.secondary, t.muted, t.surface]);

  const feedClock = clock.fi ?? clock.no;

  return (
    <div className="map-page">
      <div ref={container} className="map" />
      <aside className="panel">
        <div className="panel-head">
          <div className="live">
            <span className={connected ? "dot ok" : "dot"} aria-hidden />
            {connected ? "Live" : "Connecting…"}
            <span className="muted"> · event clock {fmtTime(feedClock)}</span>
          </div>
          <div className="stat-row">
            <Stat label="Vessels" value={vessels.current.size} />
            <Stat label="Windows" value={stats.windows ?? 0} />
            <Stat label="Truths" value={stats.truths ?? 0} />
            <Stat label="Scores" value={stats.scores ?? 0} />
          </div>
          <label className="check">
            <input type="checkbox" checked={seamarks} onChange={(e) => setSeamarks(e.target.checked)} /> OpenSeaMap seamarks
          </label>
        </div>

        {selected != null ? (
          <VesselPanel
            mmsi={selected}
            detail={detail}
            win={win}
            windowId={windowId}
            setWindowId={pickWindow}
            clock={feedClock}
            dark={dark}
            showAllK={showAllK}
            setShowAllK={setShowAllK}
            hidden={hidden}
            setHidden={setHidden}
            onClose={() => {
              setSelected(null);
              setWindowId(null);
              pinned.current = false;
            }}
          />
        ) : (
          <div className="panel-body">
            <h3>Recent verdicts</h3>
            <p className="muted small">
              Each row is one window whose 10-minute future has now happened. The number is each predictor's served ADE
              (mean distance between its chosen forecast and what the ship actually did). Click a row to see it on the map, or
              click any ship.
            </p>
            {verdicts.length === 0 && <p className="muted small">Waiting for the first truths to arrive…</p>}
            <ul className="verdicts">
              {verdicts.map((v) => {
                const entries = Object.values(v.scores).sort((a, b) => a.predictor_id.localeCompare(b.predictor_id));
                const best = entries.filter((e) => !e.miss).sort((a, b) => (a.served_ade ?? 1e18) - (b.served_ade ?? 1e18))[0];
                return (
                  <li key={v.window_id}>
                    <button className="verdict" onClick={() => openWindow(v.window_id, v.mmsi)}>
                      <span className="v-head">
                        MMSI {v.mmsi} <span className="muted">· {fmtTime(v.anchor_ts)}</span>
                      </span>
                      <span className="v-scores">
                        {entries.map((e) => (
                          <span key={e.predictor_id} className={e === best ? "chip best" : "chip"}>
                            <span className="swatch" style={{ background: predictorColor(e.predictor_id, dark) }} />
                            {e.predictor_id} {e.miss ? "miss" : fmtM(e.served_ade)}
                          </span>
                        ))}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <Legend ids={predictorIds} dark={dark} />
      </aside>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat">
      <div className="stat-value">{value.toLocaleString()}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function Legend({ ids, dark }: { ids: string[]; dark: boolean }) {
  const t = ink(dark);
  return (
    <div className="legend">
      <span className="legend-item">
        <span className="line" style={{ background: t.muted, opacity: 0.65 }} /> track (30 min)
      </span>
      <span className="legend-item">
        <span className="line" style={{ background: t.muted }} /> model input (10 min)
      </span>
      <span className="legend-item">
        <span className="line thick" style={{ background: t.primary }} /> truth
      </span>
      {ids.map((id) => (
        <span key={id} className="legend-item">
          <span className="line" style={{ background: predictorColor(id, dark) }} /> {id}
        </span>
      ))}
    </div>
  );
}

function VesselPanel(props: {
  mmsi: number;
  detail: VesselDetail | null;
  win: WindowDetail | null;
  windowId: string | null;
  setWindowId: (w: string) => void;
  clock: number | undefined;
  dark: boolean;
  showAllK: boolean;
  setShowAllK: (b: boolean) => void;
  hidden: Set<string>;
  setHidden: (s: Set<string>) => void;
  onClose: () => void;
}) {
  const { mmsi, detail, win, dark } = props;
  const v = detail?.vessel;
  const pids = win ? [...new Set([...Object.keys(win.candidates), ...Object.keys(win.scores)])].sort() : [];
  const due = win ? win.anchor_ts + HORIZON_S + TRUTH_GRACE_S : 0;
  const bestId = win
    ? Object.values(win.scores)
        .filter((s) => !s.miss)
        .sort((a, b) => (a.served_ade ?? 1e18) - (b.served_ade ?? 1e18))[0]?.predictor_id
    : undefined;

  return (
    <div className="panel-body">
      <div className="vessel-head">
        <h3>MMSI {mmsi}</h3>
        <button className="ghost" onClick={props.onClose} aria-label="Close vessel">
          ✕
        </button>
      </div>
      {v && (
        <p className="muted small">
          {v.sog ?? "–"} kn · COG {v.cog ?? "–"}° · last report {fmtTime(v.ts)}
        </p>
      )}
      {!win && (
        <p className="muted small">
          {detail?.windows.length
            ? `No current forecast: its newest window (${fmtTime(detail.windows[0].anchor_ts).slice(0, 5)}) is more than 10 minutes older than its last report. Pick a window below to see that older forecast. `
            : "No forecast for this ship yet: it needs 10 minutes of continuous, moving history, and windows are issued every 5 minutes of event time. "}
          The line shows its reported track.{" "}
          {v && v.sog != null && v.cog != null && v.sog >= MIN_MOVING_KN
            ? "The arrow is its course over ground extrapolated 5 minutes at current speed (not a model forecast)."
            : "Its speed or course is unknown or it is not moving, so there is no heading arrow."}
        </p>
      )}
      {win && (
        <p className="muted small">
          Selecting a ship shows its newest forecast and follows new windows as they arrive. Pick an earlier window below to
          pin it and compare that forecast with what the ship actually did.
        </p>
      )}
      {detail && detail.windows.length > 0 && (
        <div className="windows">
          {detail.windows.map((w) => (
            <button
              key={w.window_id}
              className={w.window_id === props.windowId ? "wchip active" : "wchip"}
              onClick={() => props.setWindowId(w.window_id)}
            >
              {fmtTime(w.anchor_ts).slice(0, 5)}{" "}
              {!w.truth ? "… pending" : w.truth.coverage < MIN_TRUTH_COVERAGE ? "✕ no truth" : "✓ scored"}
            </button>
          ))}
        </div>
      )}
      {win && (
        <>
          <p className="small">
            Anchor {fmtTime(win.anchor_ts)} ·{" "}
            {win.truth
              ? win.truth.coverage < MIN_TRUTH_COVERAGE
                ? `truth coverage ${(win.truth.coverage * 100).toFixed(0)} % — too little data after the anchor, not scored`
                : `truth coverage ${(win.truth.coverage * 100).toFixed(0)} %`
              : props.clock
                ? `truth due in ${Math.max(0, (due - props.clock) / 60).toFixed(1)} event-min`
                : "awaiting truth"}
          </p>
          <div className="tags">
            {win.tags.map((t) => (
              <span key={t} className="tag">
                {t}
              </span>
            ))}
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th>Predictor</th>
                <th className="num">Served ADE</th>
                <th className="num">Best-of-K</th>
                <th className="num">FDE</th>
                <th className="num">K</th>
              </tr>
            </thead>
            <tbody>
              {pids.map((pid) => {
                const s = win.scores[pid];
                const c = win.candidates[pid];
                const off = props.hidden.has(pid);
                return (
                  <tr key={pid} className={off ? "off" : ""}>
                    <td>
                      <label className="check">
                        <input
                          type="checkbox"
                          checked={!off}
                          onChange={() => {
                            const n = new Set(props.hidden);
                            if (off) n.delete(pid);
                            else n.add(pid);
                            props.setHidden(n);
                          }}
                        />
                        <span className="swatch" style={{ background: predictorColor(pid, dark) }} />
                        <span className={pid === bestId ? "strong" : ""}>{pid}</span>
                      </label>
                    </td>
                    <td className="num">{s ? (s.miss ? "miss" : fmtM(s.served_ade)) : win.truth ? "–" : "…"}</td>
                    <td className="num">{s && !s.miss ? fmtM(s.oracle_ade) : ""}</td>
                    <td className="num">{s && !s.miss ? fmtM(s.served_fde) : ""}</td>
                    <td className="num">{c?.paths.length ?? ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <label className="check small">
            <input type="checkbox" checked={props.showAllK} onChange={(e) => props.setShowAllK(e.target.checked)} /> Show all K
            candidates (thin), not only the served one
          </label>
        </>
      )}
    </div>
  );
}
