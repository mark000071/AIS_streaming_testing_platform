export type LonLat = [number, number];

/** [mmsi, lon, lat, sog, cog, recv_ts, hasWindow] */
export type VesselRow = [number, number, number, number | null, number | null, number, boolean];

export interface Frame {
  seq: number;
  full: boolean;
  clock: Record<string, number>;
  vessels: VesselRow[];
  removed: number[];
  events: { type: "window" | "candidates" | "truth"; mmsi: number; window_id: string; predictor_id?: string }[];
  stats: Record<string, number>;
}

export interface ScoreMsg {
  window_id: string;
  predictor_id: string;
  mmsi: number;
  feed: string;
  anchor_ts: number;
  scored_wall: number;
  miss: boolean;
  served_ade: number | null;
  served_fde: number | null;
  oracle_ade: number | null;
  k: number;
  comparable: boolean;
  coverage: number;
  scene: string;
  speed_band: string;
  queue_ms: number | null;
  compute_ms: number | null;
}

export interface CandidateSet {
  paths: (LonLat | [null, null])[][];
  scores: number[] | null;
  selected: number;
  compute_ms: number;
  queue_ms?: number;
  model_revision?: string;
}

export interface WindowDetail {
  window_id: string;
  mmsi: number;
  feed: string;
  anchor_ts: number;
  anchor: LonLat;
  tags: string[];
  history: LonLat[];
  candidates: Record<string, CandidateSet>;
  truth: { path: (LonLat | [null, null])[]; coverage: number } | null;
  scores: Record<string, ScoreMsg>;
}

export interface VesselDetail {
  vessel: { mmsi: number; feed: string; lat: number; lon: number; sog: number | null; cog: number | null; ts: number } | null;
  windows: WindowDetail[];
  /** reported positions over the last 30 min, oldest first, split into segments at long gaps and jumps */
  track?: LonLat[][];
}

export interface LeaderRow {
  predictor_id: string;
  description: string;
  k: number | null;
  n: number;
  n_scored: number;
  coverage: number;
  served_ade: number | null;
  ci95: [number | null, number | null];
  oracle_ade: number | null;
  served_fde: number | null;
  compute_p50_ms: number | null;
  per_horizon: (number | null)[];
  ranked: boolean;
}

export interface Leaderboard {
  window: string;
  stratum: string;
  rows: LeaderRow[];
  horizon_s: number[];
  latest_anchor: number | null;
}

export interface PredictorInfo {
  id: string;
  version: string;
  k: number;
  description: string;
  required_tags: string[];
  started_wall: number;
  last_beat: number;
  alive: boolean;
}

export interface HealthBeat {
  service: string;
  instance: string;
  ts: number;
  queue_depth: number;
  last_event_ts: number | null;
  parity_ok: boolean | null;
  counters: Record<string, number>;
}

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail ?? r.statusText);
  return data as T;
}

export const fmtM = (v: number | null | undefined, digits = 0) =>
  v == null || !isFinite(v) ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} km` : `${v.toFixed(digits)} m`;

export const fmtTime = (ts: number | null | undefined) =>
  ts == null ? "–" : new Date(ts * 1000).toISOString().slice(11, 19) + " UTC";
