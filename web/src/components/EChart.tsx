import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import { ink } from "../colors";

export function EChart({ option, height = 280, label }: { option: echarts.EChartsOption; height?: number; label: string }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!el.current) return;
    chart.current = echarts.init(el.current, undefined, { renderer: "svg" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true });
  }, [option]);

  return <div ref={el} style={{ height, width: "100%" }} role="img" aria-label={label} />;
}

/** Shared chrome: hairline grid, muted axes, system sans, tooltip on the chart surface. */
export function baseOption(dark: boolean): echarts.EChartsOption {
  const t = ink(dark);
  return {
    backgroundColor: "transparent",
    textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: t.secondary },
    animationDuration: 300,
    tooltip: {
      backgroundColor: t.surface,
      borderColor: dark ? "rgba(255,255,255,0.10)" : "rgba(11,11,11,0.10)",
      textStyle: { color: t.primary, fontSize: 12 },
      extraCssText: "border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);",
    },
    legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 14, itemHeight: 3, textStyle: { color: t.secondary } },
    grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
  };
}

export function axisStyle(dark: boolean) {
  const t = ink(dark);
  return {
    axisLine: { lineStyle: { color: t.axis } },
    axisTick: { show: false },
    axisLabel: { color: t.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: t.grid, width: 1 } },
    nameTextStyle: { color: t.muted, fontSize: 11 },
  };
}
