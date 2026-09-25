import { useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import { getJSON, type HealthBeat } from "../api";
import { ink, predictorColor } from "../colors";
import { EChart, axisStyle, baseOption } from "../components/EChart";

interface SlaRow {
  predictor_id: string;
  n: number;
  queue_ms: [number, number, number];
  compute_ms: [number, number, number];
  within_budget: number;
  replay: boolean;
  freshness_s: [number, number, number] | null;
  service_s: [number, number, number] | null;
}
interface Sla {
  feed: string;
  predictors: SlaRow[];
  series: Record<string, [number, number, number, number, number][]>;
  budget_s: number;
}
interface Health {
  now: number;
  services: HealthBeat[];
  clock: Record<string, number>;
  stats: Record<string, number>;
  snapshot: { built_at: number; tables: Record<string, number> };
}

const ms = (v: number | null | undefined) => (v == null ? "–" : v < 10 ? `${v.toFixed(2)} ms` : `${v.toFixed(0)} ms`);
const sec = (v: number | null | undefined) => (v == null ? "–" : `${v.toFixed(1)} s`);

const COUNTER_KEYS = ["published", "eligible", "gap", "warmup", "stationary", "truths", "scores", "predicted", "timeouts", "stale",
  "truth_insufficient", "open_windows", "progress"];

export default function SlaPage({ dark }: { dark: boolean }) {
  const [sla, setSla] = useState<Sla | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [window, setWindow] = useState("1h");

  useEffect(() => {
    let alive = true;
    const load = () => {
      getJSON<Sla>(`/v1/sla?feed=fi&window=${window}`).then((d) => alive && setSla(d)).catch(() => {});
      getJSON<Health>("/v1/health").then((d) => alive && setHealth(d)).catch(() => {});
    };
    load();
    const id = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [window]);

  const t = ink(dark);
  const option = useMemo<EChartsOption>(() => {
    const entries = Object.entries(sla?.series ?? {}).sort(([a], [b]) => a.localeCompare(b));
    return {
      ...baseOption(dark),
      tooltip: {
        ...(baseOption(dark).tooltip as object),
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: t.axis } },
        valueFormatter: (v) => ms(v as number),
      },
      xAxis: { type: "time", ...axisStyle(dark), splitLine: { show: false } },
      yAxis: { type: "value", ...axisStyle(dark), axisLabel: { color: t.muted, fontSize: 11, formatter: "{value} ms" } },
      series: entries.map(([pid, pts]) => ({
        name: pid,
        type: "line",
        data: pts.map((p) => [p[0], p[2]]),
        showSymbol: pts.length < 30,
        symbolSize: 8,
        lineStyle: { width: 2 },
        itemStyle: { color: predictorColor(pid, dark) },
      })),
    } as EChartsOption;
  }, [sla, dark, t.axis, t.muted]);

  const replay = sla?.predictors.some((p) => p.replay);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>SLA & service health</h2>
          <p className="muted small">
            Three clocks travel with every prediction: <b>anchor</b> (event time of the window), <b>enqueue</b> (tracker published
            it) and <b>issue</b> (predictor answered). Budget: answer within {sla?.budget_s ?? 5} s of enqueue, or the window
            counts as a miss.
            {replay && " In replay, anchor is historical event time, so freshness/service latency are only reported on live feeds."}
          </p>
        </div>
        <div className="filters">
          <label>
            Window
            <select value={window} onChange={(e) => setWindow(e.target.value)}>
              {["15m", "1h", "6h", "24h", "all"].map((w) => (
                <option key={w}>{w}</option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <table className="tbl wide">
        <thead>
          <tr>
            <th>Predictor</th>
            <th className="num">Predictions</th>
            <th className="num">Queue p50 / p90 / p99</th>
            <th className="num">Compute p50 / p99</th>
            <th className="num">Within 5 s</th>
            <th className="num">Freshness p50</th>
            <th className="num">Service p99</th>
          </tr>
        </thead>
        <tbody>
          {(sla?.predictors ?? []).map((p) => (
            <tr key={p.predictor_id}>
              <td>
                <span className="swatch" style={{ background: predictorColor(p.predictor_id, dark) }} />
                <span className="strong">{p.predictor_id}</span>
              </td>
              <td className="num">{p.n.toLocaleString()}</td>
              <td className="num">
                {ms(p.queue_ms[0])} / {ms(p.queue_ms[1])} / {ms(p.queue_ms[2])}
              </td>
              <td className="num">
                {ms(p.compute_ms[0])} / {ms(p.compute_ms[2])}
              </td>
              <td className="num">{(p.within_budget * 100).toFixed(2)} %</td>
              <td className="num">{sec(p.freshness_s?.[0])}</td>
              <td className="num">{sec(p.service_s?.[2])}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <section className="card">
        <h3>Queue → issue latency, p99 per bucket</h3>
        <EChart option={option} label="p99 queue latency over time per predictor" />
      </section>

      <section className="card">
        <h3>Services</h3>
        <table className="tbl wide">
          <thead>
            <tr>
              <th>Service</th>
              <th>Instance</th>
              <th className="num">Last beat</th>
              <th className="num">Backlog</th>
              <th>Parity</th>
              <th>Counters</th>
            </tr>
          </thead>
          <tbody>
            {(health?.services ?? []).map((h) => {
              const age = (health?.now ?? h.ts) - h.ts;
              return (
                <tr key={`${h.service}@${h.instance}`}>
                  <td className="strong">{h.service}</td>
                  <td className="muted small nowrap">{h.instance}</td>
                  <td className="num">
                    <span className={age < 15 ? "status good" : "status critical"}>{age < 15 ? "● ok" : "▲ stale"}</span>{" "}
                    {age.toFixed(0)} s ago
                  </td>
                  <td className="num">{h.queue_depth}</td>
                  <td>{h.parity_ok == null ? "" : h.parity_ok ? <span className="status good">● byte-parity ok</span> : "✕ failed"}</td>
                  <td className="small">
                    {Object.entries(h.counters)
                      .filter(([k]) => COUNTER_KEYS.includes(k) || k.startsWith("vessels_"))
                      .map(([k, v]) => `${k} ${k === "progress" ? `${(v * 100).toFixed(0)}%` : Math.round(v).toLocaleString()}`)
                      .join(" · ")}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {health && (
          <p className="muted small">
            Archive snapshot for DuckDB rebuilt {Math.round(health.now - health.snapshot.built_at)} s ago:{" "}
            {Object.entries(health.snapshot.tables)
              .map(([k, v]) => `${k} ${v.toLocaleString()}`)
              .join(" · ")}
          </p>
        )}
      </section>
    </div>
  );
}
