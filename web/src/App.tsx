import { useEffect, useState } from "react";
import { useTheme } from "./theme";
import MapPage from "./pages/MapPage";
import LeaderboardPage from "./pages/LeaderboardPage";
import SlaPage from "./pages/SlaPage";
import DataPage from "./pages/DataPage";

const TABS = [
  { id: "map", label: "Live map" },
  { id: "leaderboard", label: "Leaderboard" },
  { id: "sla", label: "SLA & health" },
  { id: "data", label: "Predictors & data" },
] as const;
type Tab = (typeof TABS)[number]["id"];

const fromHash = (): Tab => {
  const h = location.hash.slice(1);
  return (TABS.find((t) => t.id === h)?.id ?? "map") as Tab;
};

export default function App() {
  const [tab, setTab] = useState<Tab>(fromHash);
  const { pref, setPref, dark } = useTheme();

  useEffect(() => {
    const on = () => setTab(fromHash());
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          EnvShip <span className="brand-sub">online AIS prediction</span>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
            <a key={t.id} href={`#${t.id}`} className={tab === t.id ? "tab active" : "tab"}>
              {t.label}
            </a>
          ))}
        </nav>
        <select className="theme" value={pref} onChange={(e) => setPref(e.target.value as typeof pref)} aria-label="Theme">
          <option value="system">System theme</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </header>
      <main className="content">
        {tab === "map" && <MapPage dark={dark} />}
        {tab === "leaderboard" && <LeaderboardPage dark={dark} />}
        {tab === "sla" && <SlaPage dark={dark} />}
        {tab === "data" && <DataPage dark={dark} />}
      </main>
    </div>
  );
}
