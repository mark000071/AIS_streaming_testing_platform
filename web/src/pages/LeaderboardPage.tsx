import { useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import { fmtM, getJSON, type Leaderboard } from "../api";
import { ink, predictorColor } from "../colors";
import { EChart, axisStyle, baseOption } from "../components/EChart";

const WINDOWS = ["15m", "1h", "6h", "24h", "7d", "all"];
const STRATA: [string, string][] = [
  ["all", "All windows"],
  ["comparable", "Benchmark-comparable"],
  ["scene:straight", "Straight"],
  ["scene:turning", "Turning"],
  ["speed:slow", "Slow (< 8 kn)"],
  ["speed:medium", "Medium (8–16 kn)"],
  ["speed:fast", "Fast (≥ 16 kn)"],
];

interface Series {
  bucket_s: number;
  series: Record<string, [number, number | null, number, number | null][]>;
}

export default function LeaderboardPage({ dark }: { dark: boolean }) {
  const [window, setWindow] = useState("1h");
  const [stratum, setStratum] = useState("all");
  const [lb, setLb] = useState<Leaderboard | null>(null);
  const [ts, setTs] = useState<Series | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      getJSON<Leaderboard>(`/v1/leaderboard?window=${window}&stratum=${encodeURIComponent(stratum)}`)
        .then((d) => alive && (setLb(d), setErr(null)))
        .catch((e) => alive && setErr(String(e)));
      getJSON<Series>("/v1/leaderboard/timeseries").then((d) => alive && setTs(d)).catch(() => {});
    };
    load();
    const id = setInterval(load, 10_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [window, stratum]);

  const rows = lb?.rows ?? [];
  const t = ink(dark);

  const adeOption = useMemo<EChartsOption>(() => {
    const data = rows.filter((r) => r.served_ade != null).slice().reverse();
    return {
      ...baseOption(dark),
      legend: { show: false },
      tooltip: { ...(baseOption(dark).tooltip as object), trigger: "item" },
      grid: { left: 16, right: 24, top: 8, bottom: 24, containLabel: true },
      xAxis: {
        type: "value",
        name: "served ADE (m)",
        nameLocation: "middle",
        nameGap: 26,
        max: (v: { max: number }) =>
          Math.ceil(Math.max(v.max, ...data.map((r) => r.ci95[1] ?? 0)) * 1.05),
        ...axisStyle(dark),
      },
      yAxis: {
        type: "category",
        data: data.map((r) => r.predictor_id),
        ...axisStyle(dark),
        axisLabel: { color: t.secondary, fontSize: 12 },
        splitLine: { show: false },
      },
      series: [
        {
          type: "bar",
          barWidth: 14,
          data: data.map((r) => ({
            value: r.served_ade,
            itemStyle: { color: predictorColor(r.predictor_id, dark), borderRadius: [0, 4, 4, 0] },
            ci: r.ci95,
          })),
          tooltip: {
            formatter: (p: unknown) => {
              const q = p as { name: string; value: number; data: { ci: [number | null, number | null] } };
              const [lo, hi] = q.data.ci;
              return `<b>${q.name}</b><br/>served ADE ${fmtM(q.value)}<br/>95% CI ${fmtM(lo)} – ${fmtM(hi)}`;
            },
          },
        },
        {
          type: "custom",
          silent: true,
          z: 5,
          data: data.map((r, i) => [i, r.ci95[0] ?? r.served_ade, r.ci95[1] ?? r.served_ade]),
          renderItem: (_params, api) => {
            const y = api.value(0) as number;
            const lo = api.coord([api.value(1) as number, y]);
            const hi = api.coord([api.value(2) as number, y]);
            const style = { stroke: t.primary, lineWidth: 1.5 };
            return {
              type: "group",
              children: [
                { type: "line", shape: { x1: lo[0], y1: lo[1], x2: hi[0], y2: hi[1] }, style },
                { type: "line", shape: { x1: lo[0], y1: lo[1] - 5, x2: lo[0], y2: lo[1] + 5 }, style },
                { type: "line", shape: { x1: hi[0], y1: hi[1] - 5, x2: hi[0], y2: hi[1] + 5 }, style },
              ],
            };
          },
        },
      ],
    } as EChartsOption;
  }, [rows, dark, t.primary, t.secondary]);

  const horizonOption = useMemo<EChartsOption>(() => {
    const hs = lb?.horizon_s ?? [];
    const few = rows.length <= 4;
    return {
      ...baseOption(dark),
      tooltip: {
        ...(baseOption(dark).tooltip as object),
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: t.axis } },
        valueFormatter: (v) => fmtM(v as number),
      },
      grid: { left: 8, right: few ? 110 : 16, top: 36, bottom: 8, containLabel: true },
      xAxis: {
        type: "category",
        data: hs.map((s) => (s / 60).toFixed(s % 60 ? 1 : 0)),
        name: "horizon (min)",
        nameLocation: "middle",
        nameGap: 26,
        boundaryGap: false,
        ...axisStyle(dark),
        splitLine: { show: false },
      },
      yAxis: { type: "value", ...axisStyle(dark), axisLabel: { color: t.muted, fontSize: 11, formatter: "{value} m" } },
      series: rows.map((r) => ({
        name: r.predictor_id,
        type: "line",
        data: r.per_horizon,
        showSymbol: false,
        symbolSize: 8,
        lineStyle: { width: 2 },
        itemStyle: { color: predictorColor(r.predictor_id, dark) },
        emphasis: { focus: "series" },
        endLabel: few ? { show: true, formatter: "{a}", color: t.secondary, fontSize: 11 } : undefined,
        labelLayout: { moveOverlap: "shiftY" },
      })),
    } as EChartsOption;
  }, [rows, lb, dark, t.axis, t.secondary, t.muted]);

  const tsOption = useMemo<EChartsOption>(() => {
    const entries = Object.entries(ts?.series ?? {}).sort(([a], [b]) => a.localeCompare(b));
    return {
      ...baseOption(dark),
      tooltip: {
        ...(baseOption(dark).tooltip as object),
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: t.axis } },
        valueFormatter: (v) => fmtM(v as number),
      },
      xAxis: { type: "time", ...axisStyle(dark), splitLine: { show: false } },
      yAxis: { type: "value", ...axisStyle(dark), axisLabel: { color: t.muted, fontSize: 11, formatter: "{value} m" } },
      series: entries.map(([pid, pts]) => ({
        name: pid,
        type: "line",
        data: pts.map((p) => [p[0], p[1]]),
        showSymbol: pts.length < 30,
        symbolSize: 8,
        lineStyle: { width: 2 },
        itemStyle: { color: predictorColor(pid, dark) },
        emphasis: { focus: "series" },
      })),
    } as EChartsOption;
  }, [ts, dark, t.axis, t.muted]);

  const leader = rows.find((r) => r.ranked);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h2>Live leaderboard</h2>
          <p className="muted small">
            Every predictor sees the same windows and is scored by the same referee once the ship's real future is known. Ranked
            predictors need ≥ 95 % coverage (answers within 5 s) and ≥ 20 scored windows. Refreshes every 10 s.
          </p>
        </div>
        <div className="filters">
          <label>
            Window
            <select value={window} onChange={(e) => setWindow(e.target.value)}>
              {WINDOWS.map((w) => (
                <option key={w}>{w}</option>
              ))}
            </select>
          </label>
          <label>
            Stratum
            <select value={stratum} onChange={(e) => setStratum(e.target.value)}>
              {STRATA.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      {!rows.length && !err && (
        <p className="muted">No scored windows yet. The first truths arrive 11 minutes of event time after the first windows.</p>
      )}
      {rows.length > 0 && (
        <>
          <div className="tiles">
            <div className="tile">
              <div className="tile-label">Current leader</div>
              <div className="tile-value">{leader?.predictor_id ?? "–"}</div>
              <div className="tile-sub">{leader ? `served ADE ${fmtM(leader.served_ade)}` : "no ranked predictor yet"}</div>
            </div>
            <div className="tile">
              <div className="tile-label">Scored windows</div>
              <div className="tile-value">{Math.max(...rows.map((r) => r.n_scored)).toLocaleString()}</div>
              <div className="tile-sub">in the last {window} of event time</div>
            </div>
          </div>
          <table className="tbl wide">
            <thead>
              <tr>
                <th>#</th>
                <th>Predictor</th>
                <th className="num">Served ADE</th>
                <th className="num">95 % CI</th>
                <th className="num">Best-of-K ADE</th>
                <th className="num">Served FDE</th>
                <th className="num">Coverage</th>
                <th className="num">Scored</th>
                <th className="num">Compute p50</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.predictor_id}>
                  <td>{r.ranked ? i + 1 : "–"}</td>
                  <td>
                    <span className="swatch" style={{ background: predictorColor(r.predictor_id, dark) }} />
                    <span className="strong">{r.predictor_id}</span>
                    {r.k ? <span className="muted"> · K={r.k}</span> : null}
                    <div className="muted small">{r.description}</div>
                  </td>
                  <td className="num strong">{fmtM(r.served_ade)}</td>
                  <td className="num">
                    {fmtM(r.ci95[0])} – {fmtM(r.ci95[1])}
                  </td>
                  <td className="num">{fmtM(r.oracle_ade)}</td>
                  <td className="num">{fmtM(r.served_fde)}</td>
                  <td className="num">{(r.coverage * 100).toFixed(1)} %</td>
                  <td className="num">{r.n_scored.toLocaleString()}</td>
                  <td className="num">{r.compute_p50_ms == null ? "–" : `${r.compute_p50_ms.toFixed(1)} ms`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="grid2">
            <section className="card">
              <h3>Served ADE with bootstrap 95 % CI</h3>
              <EChart option={adeOption} height={Math.max(160, rows.length * 44 + 50)} label="Served ADE per predictor" />
            </section>
            <section className="card">
              <h3>Error by forecast horizon</h3>
              <EChart option={horizonOption} label="Mean error by horizon per predictor" />
            </section>
          </div>
          <section className="card">
            <h3>Served ADE over time ({(ts?.bucket_s ?? 300) / 60}-min buckets of event time)</h3>
            <EChart option={tsOption} label="Served ADE over time per predictor" />
          </section>
        </>
      )}
    </div>
  );
}
