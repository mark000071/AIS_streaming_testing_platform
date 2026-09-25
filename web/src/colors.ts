// Categorical slots (validated: adjacent CVD ΔE ≥ 8.4, normal-vision ≥ 19.8 in both modes).
// Colour follows the predictor, never its rank: known ids own fixed slots, new ids take the next free slot
// in first-seen order and keep it for the session.
const LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];

const slots = new Map<string, number>([
  ["cv", 0],
  ["kalman", 1],
  ["imm", 2],
  ["example-meanvel", 3],
]);

export function slotOf(id: string): number {
  let s = slots.get(id);
  if (s === undefined) {
    s = Math.min(slots.size, 7);
    slots.set(id, s);
  }
  return s;
}

export function predictorColor(id: string, dark: boolean): string {
  return (dark ? DARK : LIGHT)[slotOf(id)];
}

export function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export interface Ink {
  surface: string;
  primary: string;
  secondary: string;
  muted: string;
  grid: string;
  axis: string;
}

export const ink = (dark: boolean): Ink =>
  dark
    ? { surface: "#1a1a19", primary: "#ffffff", secondary: "#c3c2b7", muted: "#898781", grid: "#2c2c2a", axis: "#383835" }
    : { surface: "#fcfcfb", primary: "#0b0b0b", secondary: "#52514e", muted: "#898781", grid: "#e1e0d9", axis: "#c3c2b7" };
