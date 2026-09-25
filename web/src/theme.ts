import { useEffect, useState } from "react";

export type ThemePref = "system" | "light" | "dark";

function read(): ThemePref {
  try {
    const v = localStorage.getItem("envship-theme");
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(read);
  const [systemDark, setSystemDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches);

  useEffect(() => {
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => setSystemDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  useEffect(() => {
    if (pref === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", pref);
    try {
      localStorage.setItem("envship-theme", pref);
    } catch {
      /* storage unavailable: preference lasts for this page view only */
    }
  }, [pref]);

  const dark = pref === "dark" || (pref === "system" && systemDark);
  return { pref, setPref, dark };
}
