import { useEffect, useState } from "react";
import { fmtTime, getJSON, postJSON, type PredictorInfo } from "../api";
import { predictorColor } from "../colors";

interface QueryResult {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
  tables: Record<string, number>;
}

const EXAMPLES: [string, string][] = [
  [
    "Leaderboard by scene",
    `SELECT predictor_id, scene, count(*) AS n,
       round(avg(served_ade) FILTER (WHERE NOT miss), 1) AS served_ade_m,
       round(avg(oracle_ade) FILTER (WHERE NOT miss), 1) AS best_of_k_m
FROM scores
GROUP BY ALL ORDER BY scene, served_ade_m`,
  ],
  [
    "Where IMM beats CV",
    `SELECT a.window_id, a.mmsi, a.scene,
       round(c.served_ade - a.served_ade, 1) AS imm_gain_m
FROM scores a JOIN scores c USING (window_id)
WHERE a.predictor_id = 'imm' AND c.predictor_id = 'cv' AND NOT a.miss AND NOT c.miss
ORDER BY imm_gain_m DESC LIMIT 20`,
  ],
  [
    "Eligibility funnel",
    `SELECT unnest(tags) AS tag, count(*) AS windows FROM windows GROUP BY 1 ORDER BY 2 DESC`,
  ],
  [
    "Busiest vessels (raw)",
    `SELECT mmsi, count(*) AS reports, round(avg(sog), 1) AS mean_sog
FROM raw_ais GROUP BY 1 ORDER BY 2 DESC LIMIT 15`,
  ],
];

export default function DataPage({ dark }: { dark: boolean }) {
  const [preds, setPreds] = useState<PredictorInfo[]>([]);
  const [sql, setSql] = useState(EXAMPLES[0][1]);
  const [res, setRes] = useState<QueryResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const load = () => getJSON<PredictorInfo[]>("/v1/predictors").then(setPreds).catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await postJSON<QueryResult>("/v1/query", { sql }));
    } catch (e) {
      setErr(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <h2>Registered predictors</h2>
      <p className="muted small">
        Predictors are independent containers subscribed to <code>windows.eligible</code>. Adding one is a Dockerfile plus
        ~30 lines against the SDK (see <code>packages/sdk/examples/minimal.py</code>).
      </p>
      <div className="cards">
        {preds.map((p) => (
          <div key={p.id} className="card pred">
            <div className="pred-head">
              <span className="swatch big" style={{ background: predictorColor(p.id, dark) }} />
              <span className="strong">{p.id}</span>
              <span className="muted">v{p.version} · K={p.k}</span>
              <span className={p.alive ? "status good" : "status critical"}>{p.alive ? "● alive" : "▲ down"}</span>
            </div>
            <p className="small">{p.description}</p>
            <p className="muted small">
              up since {fmtTime(p.started_wall)}
              {p.required_tags.length ? ` · requires ${p.required_tags.join(", ")}` : ""}
            </p>
          </div>
        ))}
      </div>

      <h2>Query the archive (DuckDB, read-only)</h2>
      <p className="muted small">
        Tables: <code>scores</code>, <code>predictions</code>, <code>windows</code>, <code>truth</code>, <code>raw_ais</code>,{" "}
        <code>leaderboard_hourly</code>, <code>sla_hourly</code>. One SELECT per query, 10 s limit, 10k rows. The same SQL runs on
        a laptop against the published parquet files.
      </p>
      <div className="examples">
        {EXAMPLES.map(([label, q]) => (
          <button key={label} className="wchip" onClick={() => setSql(q)}>
            {label}
          </button>
        ))}
      </div>
      <textarea className="sql" value={sql} onChange={(e) => setSql(e.target.value)} rows={7} spellCheck={false} />
      <div>
        <button className="primary" onClick={run} disabled={busy}>
          {busy ? "Running…" : "Run query"}
        </button>
      </div>
      {err && <p className="error">{err}</p>}
      {res && (
        <div className="result">
          <p className="muted small">
            {res.rows.length.toLocaleString()} rows{res.truncated ? " (truncated)" : ""}
          </p>
          <div className="scroll-x">
            <table className="tbl wide">
              <thead>
                <tr>
                  {res.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {res.rows.slice(0, 500).map((r, i) => (
                  <tr key={i}>
                    {r.map((v, j) => (
                      <td key={j} className={typeof v === "number" ? "num" : ""}>
                        {v == null ? "∅" : typeof v === "object" ? JSON.stringify(v) : String(v)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
