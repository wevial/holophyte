export type Oklch = { l: number; c: number; h: number };

type Rgb = [number, number, number];

function oklchToLinear({ l, c, h }: Oklch): Rgb {
  const a = c * Math.cos((h * Math.PI) / 180);
  const b = c * Math.sin((h * Math.PI) / 180);
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_,
  ];
}

const encode = (x: number) => (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055);
const decode = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
const clamp = (x: number) => Math.min(1, Math.max(0, x));
const linear = (hex: string) => [1, 3, 5].map((at) => decode(parseInt(hex.slice(at, at + 2), 16) / 255)) as Rgb;

/** sRGB channels in 0..1, each clamped into gamut. */
export function oklchToRgb(colour: Oklch): Rgb {
  return oklchToLinear(colour).map((x) => clamp(encode(x))) as Rgb;
}

export function oklchToHex(colour: Oklch): string {
  return `#${oklchToRgb(colour).map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("")}`;
}

export function hexToOklch(hex: string): Oklch {
  const [r, g, b] = linear(hex);
  const l_ = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m_ = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s_ = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const l = 0.2104542553 * l_ + 0.793617785 * m_ - 0.0040720468 * s_;
  const a = 1.9779984951 * l_ - 2.428592205 * m_ + 0.4505937099 * s_;
  const bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.808675766 * s_;
  const h = (Math.atan2(bb, a) * 180) / Math.PI;
  return { l, c: Math.hypot(a, bb), h: h < 0 ? h + 360 : h };
}

function luminance(hex: string): number {
  const [r, g, b] = linear(hex);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2 contrast ratio of two `#rrggbb` colours, 1 to 21. */
export function contrast(first: string, second: string): number {
  const [light, dark] = [luminance(first), luminance(second)].sort((x, y) => y - x) as [number, number];
  return (light + 0.05) / (dark + 0.05);
}
