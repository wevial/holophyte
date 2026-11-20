import { useId, useState, type CSSProperties, type ReactNode } from "react";
import { SegmentedControl, SegmentedControlItem } from "@ui-sdk/react/segmented-control";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import {
  FADES, FULL, OPACITIES, resetAppearance, STRENGTHS, TEXT_SIZES, useCustomThemes, useDensity, useEffectSettings, useGrain, useHeaderEffect, useMotion, useReadingFont, useTextSize, useTheme,
  type BandEffect, type HeaderEffect, type ReadingFont,
} from "../appearance";
import { KNOBS, THEME, type ChoiceKnob } from "../effectKnobs";
import { BUILTIN_NAMES, BUILTIN_PICKS, builtinSpec, deriveTheme, isCustom, type CustomTheme, type Theme, type ThemeMode, type ThemePick } from "../../lib/theme";
import { Btn } from "./Btn";
import { MoonIcon, SunIcon, SystemIcon } from "./icons";
import { Segmented } from "./Segmented";
import { copyOf, ThemeEditor, type EditorSubject } from "./ThemeEditor";

const SCHEMES: { value: Theme; name: string; icon: ReactNode }[] = [
  { value: "system", name: "System", icon: <SystemIcon /> },
  { value: "light", name: "Light", icon: <SunIcon /> },
  { value: "dark", name: "Dark", icon: <MoonIcon /> },
];


const EFFECTS: { value: HeaderEffect; name: string; still?: string }[] = [
  { value: "haze", name: "Haze", still: "/effects/haze.webp" },
  { value: "caustics", name: "Caustics", still: "/effects/caustics.webp" },
  { value: "plume", name: "Plume", still: "/effects/plume.webp" },
  { value: "ribbons", name: "Ribbons", still: "/effects/ribbons.webp" },
  { value: "lava", name: "Lava", still: "/effects/lava.webp" },
  { value: "hyphae", name: "Hyphae", still: "/effects/hyphae.webp" },
  { value: "none", name: "None" },
];

const DENSITIES = [
  { value: "comfortable", label: "Comfortable" },
  { value: "compact", label: "Compact" },
] as const;

const CARD = "rounded-[10px] bg-raised shadow-[inset_0_0_0_1px_var(--hair)]";

const FONTS: { value: ReadingFont; name: string; line: string }[] = [
  { value: "sans", name: "Plex Sans", line: "default" },
  { value: "serif", name: "Plex Serif", line: "for long reads" },
  { value: "hyperlegible", name: "Hyperlegible", line: "clearest letterforms" },
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
const KnobsGlyph = () => <Glyph><path d="M2.5 4.5h6M11.5 4.5h2M2.5 11.5h2M7.5 11.5h6M10 3v3M6 10v3" /></Glyph>;
const ChevronGlyph = ({ up }: { up: boolean }) => <Glyph><path d={up ? "M5 9.5 8 6.5l3 3" : "M5 6.5 8 9.5l3-3"} /></Glyph>;
const DensityGlyph = () => <Glyph><path d="M2.5 4h11M2.5 8h11M2.5 12h11" /></Glyph>;

function Row({ id, icon, title, children, reset, labelTop = "pt-2" }: {
  id: string; icon: ReactNode; title: string; children: ReactNode; reset?: ReactNode; labelTop?: string;
}) {
  return (
    <div className="grid gap-x-3 gap-y-3 py-4 min-[860px]:grid-cols-[84px_minmax(0,1fr)]">
      <div className={`flex items-center gap-1 self-start ${labelTop}`}>
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

const TILE_TOKENS = ["--canvas", "--side", "--accent", "--haze-back", "--haze-front"];

function tileStyle(theme: CustomTheme): CSSProperties {
  const { tokens } = deriveTheme(theme);
  return Object.fromEntries(TILE_TOKENS.map((token) => [token, tokens[token]]));
}

function ThemeTile({ name, pressed, onPick, tile, style, custom = false }: {
  name: string; pressed: boolean; onPick: () => void; tile?: string; style?: CSSProperties; custom?: boolean;
}) {
  return (
    <button type="button" aria-pressed={pressed} onClick={onPick} className="group flex min-w-0 flex-col gap-1.5 text-left min-[720px]:w-[122px]">
      <span aria-hidden="true" data-tile={tile} style={style}
        className={`theme-tile relative block h-[58px] w-full rounded-card shadow-[inset_0_0_0_1px_var(--hair)] ${pressed ? "outline-2 outline-offset-2 outline-focus" : "group-hover:shadow-[inset_0_0_0_1px_var(--hair-2)]"}`}>
        <span className="theme-tile-dot absolute right-2 bottom-2 size-2.5 rounded-full" />
      </span>
      <span className="flex min-w-0 items-baseline gap-2">
        <span className={`truncate text-[13px] ${pressed ? "text-ink" : "text-ink-2"}`}>{name}</span>
        {custom && <span className="font-sans text-[12px] text-ink-3"> custom</span>}
      </span>
    </button>
  );
}

function ThemeRow({ mode, onSubject }: { mode: ThemeMode; onSubject: (subject: EditorSubject) => void }) {
  const { lightPick, setLightPick, darkPick, setDarkPick } = useTheme();
  const { customThemes } = useCustomThemes();
  const [picked, pick] = mode === "light" ? [lightPick, setLightPick] : [darkPick, setDarkPick];
  const choose = (value: ThemePick) => { pick(value); onSubject({ mode, pick: value }); };
  const customs = customThemes.filter((theme) => deriveTheme(theme).mode === mode);
  const pickedTheme = isCustom(picked) ? customs.find((theme) => theme.id === picked) : builtinSpec(mode, picked);
  return (
    <Row id={`appearance-${mode}`} icon={mode === "light" ? <SunIcon /> : <MoonIcon />} title={mode === "light" ? "Light" : "Dark"} labelTop="pt-[15px]">
      <div role="group" aria-labelledby={`appearance-${mode}`} className="grid grid-cols-3 gap-3 min-[720px]:flex min-[720px]:flex-wrap">
        {BUILTIN_PICKS.map((value) => (
          <ThemeTile key={value} name={BUILTIN_NAMES[value]} pressed={picked === value} tile={`${mode}-${value}`} onPick={() => choose(value)} />
        ))}
        {customs.map((theme) => (
          <ThemeTile key={theme.id} name={theme.name} pressed={picked === theme.id} style={tileStyle(theme)} custom onPick={() => choose(theme.id)} />
        ))}
        <button type="button" onClick={() => onSubject({ draft: copyOf(pickedTheme ?? builtinSpec(mode, "lotus")) })}
          className="flex h-[58px] flex-col items-center justify-center gap-1 rounded-card text-[13px] text-ink-2 hover:bg-hover hover:text-ink min-[720px]:w-[122px]">
          <span aria-hidden="true" className="text-[18px] leading-none">+</span>New theme
        </button>
      </div>
    </Row>
  );
}

function EffectRow() {
  const { effect, setEffect } = useHeaderEffect();
  const { motion, reduced, setMotion } = useMotion();
  const [open, setOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const section = useId();
  const shown = effect !== "none" && (open || closing);
  const toggle = () => {
    setClosing(open && !reduced);
    setOpen(!open);
  };
  return (
    <Row id="appearance-header" icon={<WavesGlyph />} title="Header" labelTop="pt-1.5">
      <div className="flex flex-wrap items-start gap-3">
        <div role="group" aria-labelledby="appearance-header" className="w-full grid grid-cols-3 gap-3 min-[720px]:flex min-[720px]:flex-wrap min-[720px]:w-auto">
          {EFFECTS.map((item) => {
            const pressed = effect === item.value;
            return (
              <button key={item.value} type="button" aria-pressed={pressed} onClick={() => { setEffect(item.value); setClosing(false); }} className="group flex min-w-0 flex-col gap-1.5 text-left min-[720px]:w-[96px]">
                <span aria-hidden="true"
                  className={`relative block h-[40px] w-full overflow-hidden rounded-card bg-canvas shadow-[inset_0_0_0_1px_var(--hair)] ${item.still ? "after:absolute after:inset-0 after:rounded-card after:shadow-[inherit] after:content-['']" : ""} ${pressed ? "outline-2 outline-offset-2 outline-focus" : "group-hover:shadow-[inset_0_0_0_1px_var(--hair-2)]"}`}>
                  {item.still && <img src={item.still} alt="" draggable={false} className="size-full object-cover" />}
                </span>
                <span className={`text-[13px] ${pressed ? "text-ink" : "text-ink-2"}`}>{item.name}</span>
              </button>
            );
          })}
        </div>
        <label className="flex min-h-11 items-center gap-2.5 pr-[3px] text-[13px] text-ink-2 min-[720px]:pl-[22px]">
          <Switch aria-label="Motion" checked={motion && !reduced} disabled={reduced} onCheckedChange={setMotion} className="switch">
            <SwitchThumb />
          </Switch>
          <span aria-hidden="true">Motion</span>
          {reduced && <span className="font-sans text-[12px] text-ink-3">off under reduced motion</span>}
        </label>
        {effect !== "none" && (
          <button type="button" aria-expanded={open} aria-controls={shown ? section : undefined} onClick={toggle}
            className={`mt-1 flex h-9 items-center gap-2 rounded-button px-2.5 text-[13px] font-medium ${open ? "bg-selected text-ink" : "text-ink-2 hover:text-ink"}`}>
            <KnobsGlyph />Customize<ChevronGlyph up={open} />
          </button>
        )}
      </div>
      {shown && (
        <div id={section} inert={!open} onTransitionEnd={(event) => { if (event.target === event.currentTarget && !open) setClosing(false); }}
          className={`grid transition-[grid-template-rows,opacity] duration-250 ease-out transition-discrete motion-reduce:transition-none ${open ? "grid-rows-[1fr] opacity-100 starting:grid-rows-[0fr] starting:opacity-0" : "grid-rows-[0fr] opacity-0"}`}>
          <div className="min-h-0 overflow-hidden">
            <EffectCustomize key={effect} effect={effect} />
          </div>
        </div>
      )}
    </Row>
  );
}

const EFFECT_NAMES = Object.fromEntries(EFFECTS.map((item) => [item.value, item.name])) as Record<HeaderEffect, string>;

const decimals = (step: number) => String(step).split(".")[1]?.length ?? 0;

function KnobSlider({ label, value, range, onChange, unit = "" }: {
  label: string; value: number; range: { min: number; max: number; step: number }; onChange: (value: number) => void; unit?: string;
}) {
  const shown = unit ? `${value}${unit}` : value.toFixed(decimals(range.step));
  return (
    <div className="grid h-[38px] grid-cols-[77px_minmax(0,1fr)_52px] items-center gap-3">
      <span aria-hidden="true" className="text-[13px] text-ink-2">{label}</span>
      <input type="range" aria-label={label} min={range.min} max={range.max} step={range.step} value={value}
        onChange={(event) => onChange(Number(event.currentTarget.value))} className="h-[38px] min-w-0 accent-act" />
      <output aria-label={`${label} value`} className="rounded-pill border border-rule bg-paper-2 py-0.5 text-center font-mono text-[12px] text-ink-2">{shown}</output>
    </div>
  );
}

function KnobChoice({ knob, value, onChange }: { knob: ChoiceKnob; value: string; onChange: (value: string) => void }) {
  return (
    <div className="grid min-h-[38px] grid-cols-[77px_minmax(0,1fr)] items-center gap-3 min-[1100px]:col-span-2">
      <span aria-hidden="true" className="text-[13px] text-ink-2">{knob.label}</span>
      <div role="group" aria-label={knob.label} className="flex h-[38px] min-w-0 items-stretch gap-1 overflow-x-auto rounded-[7px] bg-paper-2 p-[3px] shadow-[inset_0_0_0_1px_var(--hair-2)]">
        {knob.choices.map((choice) => (
          <button key={choice} type="button" aria-pressed={choice === value} onClick={() => onChange(choice)}
            className="shrink-0 rounded-[5px] px-2.5 font-sans text-[15px] text-ink-3 transition-colors hover:text-ink aria-pressed:bg-selected aria-pressed:font-medium aria-pressed:text-ink aria-pressed:shadow-[inset_0_0_0_1px_var(--hair-2)]">
            {choice === THEME ? "Theme" : choice}
          </button>
        ))}
      </div>
    </div>
  );
}

function EffectCustomize({ effect }: { effect: BandEffect }) {
  const { values, set, changed, reset } = useEffectSettings(effect);
  const heading = useId();
  return (
    <section aria-labelledby={heading} className={`mt-4 max-w-[860px] px-4 pt-3 pb-4 ${CARD}`}>
      <div className="flex items-center justify-between">
        <h4 id={heading} className="font-sans text-[13px] font-semibold text-ink">{EFFECT_NAMES[effect]}</h4>
        <button type="button" disabled={!changed} onClick={reset} className="flex min-h-9 items-center gap-1.5 px-1 text-[13px] text-ink-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-ink-2">
          <ResetGlyph />Reset
        </button>
      </div>
      <div className="mt-2.5 grid gap-x-7 gap-y-1 min-[1100px]:grid-cols-2">
        <KnobSlider label="Fade" value={Number(values.fade)} range={FADES} unit="%" onChange={(value) => set("fade", value)} />
        <KnobSlider label="Opacity" value={Number(values.opacity)} range={OPACITIES} unit="%" onChange={(value) => set("opacity", value)} />
        {effect === "haze" && <KnobSlider label="Strength" value={Number(values.strength)} range={STRENGTHS} unit="%" onChange={(value) => set("strength", value)} />}
        {KNOBS[effect].map((knob) => knob.kind === "choice"
          ? <KnobChoice key={knob.key} knob={knob} value={String(values[knob.key])} onChange={(value) => set(knob.key, value)} />
          : <KnobSlider key={knob.key} label={knob.label} value={Number(values[knob.key])} range={knob} onChange={(value) => set(knob.key, value)} />)}
      </div>
    </section>
  );
}

function SliderRow({ id, icon, title, label, value, range, onChange, pillFirst = false, initial = FULL }: {
  id: string; icon: ReactNode; title: string; label: string; value: number; range: typeof STRENGTHS; onChange: (value: number) => void; pillFirst?: boolean; initial?: number;
}) {
  const slider = <input type="range" aria-label={label} min={range.min} max={range.max} step={range.step} value={value}
    onChange={(event) => onChange(Number(event.currentTarget.value))} className="h-11 min-w-0 flex-1 accent-act" />;
  const pill = <output aria-label={`${label} value`} className={`rounded-pill border border-rule bg-paper-2 px-2 py-0.5 text-center font-mono text-[12px] text-ink-2 ${pillFirst ? "w-[52px] shrink-0" : "min-w-[56px]"}`}>{value}%</output>;
  return (
    <Row id={id} icon={icon} title={title} reset={value !== initial && <RowReset label={`Reset ${label.toLowerCase()}`} onReset={() => onChange(initial)} />}>
      <div className={`flex items-center gap-4 ${pillFirst ? "max-w-[460px]" : "max-w-[520px]"}`}>
        {pillFirst ? <>{pill}{slider}</> : <>{slider}{pill}</>}
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
          <label key={item.value} className={`min-w-0 cursor-pointer rounded-[10px] px-3.5 py-3 shadow-[inset_0_0_0_1px_var(--hair)] transition-colors focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus ${font === item.value ? "bg-selected" : "bg-raised hover:bg-hover"}`}>
            <span className="flex items-center gap-2 font-sans text-[13px] font-semibold text-ink">
              <input type="radio" name="reading-font" aria-label={item.name} checked={font === item.value} onChange={() => setFont(item.value)}
                className="grid size-4 shrink-0 cursor-pointer appearance-none place-items-center rounded-full shadow-[inset_0_0_0_1.5px_var(--hair-2)] before:size-2 before:rounded-full before:content-[''] checked:bg-accent checked:shadow-none checked:before:bg-raised forced-colors:appearance-auto" />
              {item.name}
            </span>
            <p data-font-sample={item.value} className="mt-2 font-read text-[15px]">The sweep frees a silent lease.</p>
            <p className="mt-3 font-sans text-[12.5px] text-ink-3">{item.line}</p>
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
      <p id="appearance-density-help" className="mb-3 font-sans text-[13px] text-ink-3">Compact tightens rows on Overview, Runs and Tickets.</p>
      <Segmented label="Density" options={DENSITIES} value={density} onPick={setDensity} describedBy="appearance-density-help" />
      <div role="region" aria-label="Preview" className={`mt-4 ${CARD}`}>
        {[0, 1].map((row) => (
          <div key={row} data-row className="flex min-h-[66px] flex-wrap items-center gap-x-3 gap-y-1 border-hair px-3.5 py-3.5 not-first:border-t">
            <span className="inline-flex items-center gap-1.5 text-live">
              <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-3.5 shrink-0">
                <circle cx="8" cy="8" r="6.5" />
                <path d="M8 5.4a2.6 2.6 0 1 0 0 5.2a2.6 2.6 0 1 0 0-5.2z" fill="currentColor" stroke="none" />
              </svg>
              <span className="font-sans text-[13px] font-medium">reviewing</span>
            </span>
            <span className="font-mono text-[12px] text-ink-2">HOLO-231</span>
            <span className="font-read text-[14.5px] text-ink">The sweep frees a silent lease</span>
            <span className="ml-auto font-mono text-[12px] text-ink-3">16m / 25m</span>
          </div>
        ))}
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

export function AppearanceHeader() {
  const { theme, setTheme } = useTheme();
  return (
    <div className="ml-auto flex flex-wrap items-center gap-3 self-center">
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
  );
}

export function AppearanceTab() {
  const { resolvedTheme, lightPick, darkPick } = useTheme();
  const [subject, setSubject] = useState<EditorSubject>(() => ({ mode: resolvedTheme, pick: resolvedTheme === "dark" ? darkPick : lightPick }));
  const { textSize, setTextSize } = useTextSize();
  const { grain, setGrain } = useGrain();
  return (
    <div>
      <ThemeRow mode="light" onSubject={setSubject} />
      <ThemeRow mode="dark" onSubject={setSubject} />
      <EffectRow />
      <SliderRow id="appearance-text" icon={<TextGlyph />} title="Text" label="Text size" value={textSize} range={TEXT_SIZES} onChange={setTextSize} pillFirst />
      <SliderRow id="appearance-grain" icon={<GrainGlyph />} title="Grain" label="Grain" value={grain} range={STRENGTHS} onChange={setGrain} />
      <div className="mt-2 border-t border-rule-2">
        <FontRow />
        <DensityRow />
      </div>
      <ThemeEditor subject={subject} onSubject={setSubject} />
    </div>
  );
}
