import { useState, type ReactNode } from "react";
import { SegmentedControl, SegmentedControlItem } from "@ui-sdk/react/segmented-control";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import {
  FULL, resetAppearance, STRENGTHS, TEXT_SIZES, useDensity, useGrain, useHazeStrength, useHeaderEffect, useMotion, useReadingFont, useTextSize, useTheme,
  type HeaderEffect, type ReadingFont,
} from "../appearance";
import type { Theme, ThemeMode, ThemePick } from "../../lib/theme";
import { Btn } from "./Btn";
import { MoonIcon, SunIcon, SystemIcon } from "./icons";

const SCHEMES: { value: Theme; name: string; icon: ReactNode }[] = [
  { value: "system", name: "System", icon: <SystemIcon /> },
  { value: "light", name: "Light", icon: <SunIcon /> },
  { value: "dark", name: "Dark", icon: <MoonIcon /> },
];

const PICKS: { value: ThemePick; name: string }[] = [
  { value: "lotus", name: "Lotus" },
  { value: "dune", name: "Dune" },
  { value: "tide", name: "Tide" },
  { value: "mono", name: "Mono" },
  { value: "forest", name: "Forest" },
];

const EFFECTS: { value: HeaderEffect | "caustics"; name: string; preview: string }[] = [
  { value: "haze", name: "Haze", preview: "effect-haze" },
  { value: "caustics", name: "Caustics", preview: "effect-caustics" },
  { value: "none", name: "None", preview: "bg-canvas" },
];

const FONTS: { value: ReadingFont; name: string; line: string; face: string }[] = [
  { value: "sans", name: "Plex Sans", line: "default", face: '"IBM Plex Sans", Arial, sans-serif' },
  { value: "serif", name: "Plex Serif", line: "for long reads", face: '"IBM Plex Serif", Georgia, serif' },
  { value: "hyperlegible", name: "Hyperlegible", line: "clearest letterforms", face: '"Atkinson Hyperlegible Next", Arial, sans-serif' },
];

function Glyph({ children }: { children: ReactNode }) {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-4 shrink-0">
      {children}
    </svg>
  );
}

const ResetGlyph = () => <Glyph><path d="M3 8a5 5 0 1 0 1.6-3.7M3 2.5v2.5h2.5" /></Glyph>;
const WavesGlyph = () => <Glyph><path d="M2 6.5c2-1.5 4 1.5 6 0s4 1.5 6 0M2 10.5c2-1.5 4 1.5 6 0s4 1.5 6 0" /></Glyph>;
const TextGlyph = () => <Glyph><path d="M1.5 13 5 3.5 8.5 13M2.8 9.5h4.4M10 13l2.2-6 2.3 6M10.7 11h3" /></Glyph>;
const GrainGlyph = () => <Glyph><path d="M3 3h.01M8 3h.01M13 3h.01M5.5 6h.01M10.5 6h.01M3 9h.01M8 9h.01M13 9h.01M5.5 12h.01M10.5 12h.01" /></Glyph>;
const FontGlyph = () => <Glyph><path d="M3 13h4M5 13V3h6M5 8h4.5" /></Glyph>;
const DensityGlyph = () => <Glyph><path d="M2.5 4h11M2.5 8h11M2.5 12h11" /></Glyph>;

function Row({ id, icon, title, children, reset }: { id: string; icon: ReactNode; title: string; children: ReactNode; reset?: ReactNode }) {
  return (
    <div className="grid gap-x-6 gap-y-3 py-4 min-[860px]:grid-cols-[130px_minmax(0,1fr)]">
      <div className="flex items-center gap-2 self-start">
        <h3 id={id} className="flex min-h-7 items-center gap-2 font-sans text-[14px] font-semibold text-ink">
          <span className="text-ink-2">{icon}</span>{title}
        </h3>
        {reset}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function RowReset({ label, onReset }: { label: string; onReset: () => void }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onReset} className="ml-auto flex size-7 items-center justify-center text-ink-3 hover:text-ink">
      <ResetGlyph />
    </button>
  );
}

function ThemeRow({ mode }: { mode: ThemeMode }) {
  const { lightPick, setLightPick, darkPick, setDarkPick } = useTheme();
  const [picked, pick] = mode === "light" ? [lightPick, setLightPick] : [darkPick, setDarkPick];
  return (
    <Row id={`appearance-${mode}`} icon={mode === "light" ? <SunIcon /> : <MoonIcon />} title={mode === "light" ? "Light" : "Dark"}>
      <div role="group" aria-labelledby={`appearance-${mode}`} className="flex flex-wrap gap-3">
        {PICKS.map((item) => (
          <button key={item.value} type="button" aria-pressed={picked === item.value} onClick={() => pick(item.value)}
            className="group flex w-[122px] flex-col gap-1.5 text-left">
            <span aria-hidden="true" data-tile={`${mode}-${item.value}`}
              className={`theme-tile relative block h-[58px] w-full rounded-card border ${picked === item.value ? "border-focus outline-2 outline-offset-2 outline-focus" : "border-rule group-hover:border-ink-3"}`}>
              <span className="theme-tile-dot absolute right-2 bottom-2 size-2.5 rounded-full" />
            </span>
            <span className={`text-[13px] ${picked === item.value ? "text-ink" : "text-ink-2"}`}>{item.name}</span>
          </button>
        ))}
      </div>
    </Row>
  );
}

function EffectRow() {
  const { effect, setEffect } = useHeaderEffect();
  const { motion, reduced, setMotion } = useMotion();
  return (
    <Row id="appearance-header" icon={<WavesGlyph />} title="Header">
      <div className="flex flex-wrap items-start gap-3">
        <div role="group" aria-labelledby="appearance-header" className="flex flex-wrap gap-3">
          {EFFECTS.map((item) => {
            const soon = item.value === "caustics";
            const pressed = effect === item.value;
            return (
              <button key={item.value} type="button" aria-pressed={pressed} disabled={soon} onClick={() => { if (item.value !== "caustics") setEffect(item.value); }}
                className="group flex w-[96px] flex-col gap-1.5 text-left disabled:cursor-not-allowed">
                <span aria-hidden="true" className={`${item.preview} block h-[40px] w-full rounded-card border ${pressed ? "border-focus outline-2 outline-offset-2 outline-focus" : "border-rule"} ${soon ? "opacity-50" : "group-hover:border-ink-3"}`} />
                <span className={`flex items-baseline gap-2 text-[13px] ${pressed ? "text-ink" : soon ? "text-ink-3" : "text-ink-2"}`}>
                  {item.name}{soon && <span className="font-mono text-[11px] text-ink-3">soon</span>}
                </span>
              </button>
            );
          })}
        </div>
        <label className="flex min-h-11 items-center gap-2.5 pl-3 text-[13px] text-ink-2">
          <Switch aria-label="Motion" checked={motion && !reduced} disabled={reduced} onCheckedChange={setMotion}
            className="relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-rule bg-hover data-checked:border-act data-checked:bg-act disabled:opacity-50">
            <SwitchThumb className="absolute left-0.5 size-3.5 rounded-full bg-ink-3 transition-transform data-checked:translate-x-4 data-checked:bg-act-ink" />
          </Switch>
          <span aria-hidden="true">Motion</span>
          {reduced && <span className="font-mono text-[11px] text-ink-3">off under reduced motion</span>}
        </label>
      </div>
    </Row>
  );
}

function SliderRow({ id, icon, title, label, value, range, onChange }: {
  id: string; icon: ReactNode; title: string; label: string; value: number; range: typeof STRENGTHS; onChange: (value: number) => void;
}) {
  return (
    <Row id={id} icon={icon} title={title} reset={value !== FULL && <RowReset label={`Reset ${label.toLowerCase()}`} onReset={() => onChange(FULL)} />}>
      <div className="flex max-w-[520px] items-center gap-4">
        <input type="range" aria-label={label} min={range.min} max={range.max} step={range.step} value={value}
          onChange={(event) => onChange(Number(event.currentTarget.value))} className="h-11 min-w-0 flex-1 accent-act" />
        <output aria-label={`${label} value`} className="min-w-[56px] rounded-pill border border-rule bg-paper-2 px-2 py-0.5 text-center font-mono text-[12px] text-ink-2">{value}%</output>
      </div>
    </Row>
  );
}

function FontRow() {
  const { font, setFont } = useReadingFont();
  return (
    <Row id="appearance-font" icon={<FontGlyph />} title="Reading font" reset={font !== "sans" && <RowReset label="Reset reading font" onReset={() => setFont("sans")} />}>
      <p id="appearance-font-help" className="mb-3 font-sans text-[13px] text-ink-3">For titles, summaries and agent text. Keys, times and counts stay in Plex Mono.</p>
      <div role="radiogroup" aria-labelledby="appearance-font" aria-describedby="appearance-font-help" className="grid gap-3 min-[1100px]:grid-cols-3">
        {FONTS.map((item) => (
          <label key={item.value} className={`min-w-0 cursor-pointer rounded-card border p-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus ${font === item.value ? "border-act bg-act-wash" : "border-rule bg-paper-2 hover:border-ink-3"}`}>
            <span className="flex items-center gap-2 font-mono text-[12px] font-medium uppercase tracking-[0.06em]">
              <input type="radio" name="reading-font" aria-label={item.name} checked={font === item.value} onChange={() => setFont(item.value)} className="size-3 shrink-0 accent-act" />
              {item.name}
            </span>
            <p className="mt-2 text-[15px]" style={{ fontFamily: item.face }}>The sweep frees a silent lease.</p>
            <p className="mt-3 font-mono text-[12px] text-ink-3">{item.line}</p>
          </label>
        ))}
      </div>
    </Row>
  );
}

function DensityRow() {
  const { density, setDensity } = useDensity();
  return (
    <Row id="appearance-density" icon={<DensityGlyph />} title="Density" reset={density !== "comfortable" && <RowReset label="Reset density" onReset={() => setDensity("comfortable")} />}>
      <p id="appearance-density-help" className="mb-3 font-sans text-[13px] text-ink-3">Compact tightens rows on Overview, Runs and Tickets. Buttons keep their 44px.</p>
      <div role="group" aria-labelledby="appearance-density" aria-describedby="appearance-density-help" className="inline-flex border border-rule">
        {(["comfortable", "compact"] as const).map((value) => (
          <button key={value} type="button" aria-pressed={density === value} onClick={() => setDensity(value)}
            className={`min-h-11 px-4 font-mono text-[12px] ${density === value ? "bg-act text-act-ink" : "bg-paper text-ink-2 hover:bg-paper-2"}`}>
            {value === "comfortable" ? "Comfortable" : "Compact"}
          </button>
        ))}
      </div>
      <div role="region" aria-label="Preview" className="mt-4">
        <p className="mb-2 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">Preview</p>
        <div className="border border-rule bg-paper">
          {[0, 1].map((row) => (
            <div key={row} data-row className="flex min-h-[66px] flex-wrap items-center gap-x-3 gap-y-1 border-rule-2 px-3 py-3.5 not-first:border-t">
              <span className="font-mono text-[12px] text-live">reviewing</span>
              <span className="font-mono text-[12px] text-ink-2">HOLO-231</span>
              <span className="font-read text-[14.5px] text-ink">The sweep frees a silent lease</span>
              <span className="ml-auto font-mono text-[12px] text-ink-3">16m / 25m</span>
            </div>
          ))}
        </div>
      </div>
    </Row>
  );
}

function PageReset() {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button type="button" onClick={() => setConfirming(true)} className="flex min-h-11 items-center gap-2 px-2 text-[13px] text-ink-2 hover:text-ink">
        <ResetGlyph />Reset to default
      </button>
    );
  }
  return (
    <div role="group" aria-label="Reset to default" className="flex items-center gap-2">
      <span className="text-[13px] text-ink">Reset?</span>
      <Btn variant="primary" onClick={() => { resetAppearance(); setConfirming(false); }}>Confirm</Btn>
      <Btn onClick={() => setConfirming(false)}>Cancel</Btn>
    </div>
  );
}

export function AppearanceTab() {
  const { theme, setTheme } = useTheme();
  const { textSize, setTextSize } = useTextSize();
  const { grain, setGrain } = useGrain();
  const { hazeStrength, setHazeStrength } = useHazeStrength();
  return (
    <div>
      <div className="flex flex-wrap items-center justify-end gap-3 pb-2">
        <SegmentedControl<Theme> aria-label="Theme" value={theme} onValueChange={(value) => { if (value) setTheme(value); }} className="inline-flex rounded-button border border-rule bg-paper-2 p-0.5">
          {SCHEMES.map((item) => (
            <SegmentedControlItem key={item.value} value={item.value}
              className="flex min-h-9 items-center gap-1.5 rounded-button px-3 text-[13px] text-ink-3 hover:text-ink data-checked:bg-raised data-checked:text-ink">
              {item.icon}{item.name}
            </SegmentedControlItem>
          ))}
        </SegmentedControl>
        <PageReset />
      </div>
      <ThemeRow mode="light" />
      <ThemeRow mode="dark" />
      <EffectRow />
      <SliderRow id="appearance-text" icon={<TextGlyph />} title="Text" label="Text size" value={textSize} range={TEXT_SIZES} onChange={setTextSize} />
      <SliderRow id="appearance-grain" icon={<GrainGlyph />} title="Grain" label="Grain" value={grain} range={STRENGTHS} onChange={setGrain} />
      <SliderRow id="appearance-haze" icon={<WavesGlyph />} title="Haze" label="Haze strength" value={hazeStrength} range={STRENGTHS} onChange={setHazeStrength} />
      <div className="mt-2 border-t border-rule-2">
        <FontRow />
        <DensityRow />
      </div>
    </div>
  );
}
