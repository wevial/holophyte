import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { oklchToHex, oklchToRgb, type Oklch } from "../../lib/oklch";
import {
  builtinSpec, buttonInk, contrastChecks, deriveTheme, isCustom, type CustomTheme, type DerivedTheme, type HazeSource, type ThemeMode, type ThemePick, type ThemeSpec,
} from "../../lib/theme";
import { useCustomThemes } from "../appearance";
import { Btn } from "./Btn";

export type EditorSubject = { mode: ThemeMode; pick: ThemePick } | { draft: CustomTheme };

const GROUND_CHROMA = 0.1;
const ACCENT_CHROMA = 0.32;
const GRAIN = { min: 0, max: 8, step: 0.5 };

const round = (x: number) => Math.round(x * 1000) / 1000;
const clamp = (x: number, max = 1) => Math.min(max, Math.max(0, x));

export function copyOf(spec: ThemeSpec): CustomTheme {
  return { ...spec, name: `${spec.name} copy`, id: `custom:${crypto.randomUUID().slice(0, 8)}`, origin: spec.accent };
}

/** The subject's theme as the editor holds it, `saved` when it is a stored custom theme. */
function subjectTheme(subject: EditorSubject, customs: CustomTheme[]): { theme: CustomTheme | ThemeSpec; saved: CustomTheme | null } | null {
  if ("draft" in subject) return { theme: subject.draft, saved: null };
  if (!isCustom(subject.pick)) return { theme: builtinSpec(subject.mode, subject.pick), saved: null };
  const saved = customs.find((theme) => theme.id === subject.pick);
  return saved ? { theme: saved, saved } : null;
}

const hueStops = Array.from({ length: 13 }, (_, step) => oklchToHex({ l: 0.72, c: 0.14, h: step * 30 })).join(", ");

function HueStrip({ label, hue, disabled, onChange }: { label: string; hue: number; disabled: boolean; onChange: (hue: number) => void }) {
  return (
    <input type="range" aria-label={label} min={0} max={359} step={1} value={Math.round(hue)} disabled={disabled}
      onChange={(event) => onChange(Number(event.currentTarget.value))}
      style={{ background: `linear-gradient(to right, ${hueStops})` }}
      className="h-3 w-full cursor-pointer appearance-none rounded-pill disabled:cursor-not-allowed disabled:opacity-60 [&::-moz-range-thumb]:size-4 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-white [&::-moz-range-thumb]:bg-transparent [&::-webkit-slider-thumb]:size-4 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-white [&::-webkit-slider-thumb]:shadow-[0_0_0_1px_rgba(0,0,0,.35)]" />
  );
}

function drawSquare(canvas: HTMLCanvasElement | null, hue: number, maxChroma: number) {
  const context = canvas?.getContext?.("2d");
  if (!canvas || !context) return;
  const { width, height } = canvas;
  const image = context.createImageData(width, height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const rgb = oklchToRgb({ l: 1 - (y + 0.5) / height, c: ((x + 0.5) / width) * maxChroma, h: hue });
      image.data.set([...rgb.map((channel) => Math.round(channel * 255)), 255], (y * width + x) * 4);
    }
  }
  context.putImageData(image, 0, 0);
}

const SQUARE_KEYS: Record<string, [lightness: number, chroma: number]> = {
  ArrowUp: [0.01, 0], ArrowDown: [-0.01, 0], ArrowRight: [0, 0.005], ArrowLeft: [0, -0.005],
};

function ColourSquare({ label, value, maxChroma, disabled, onChange }: {
  label: string; value: Oklch; maxChroma: number; disabled: boolean; onChange: (value: Oklch) => void;
}) {
  const area = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => drawSquare(canvas.current, value.h, maxChroma), [value.h, maxChroma]);
  const fromPointer = (event: PointerEvent<HTMLDivElement>) => {
    const rect = area.current!.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    onChange({ ...value, l: round(1 - clamp((event.clientY - rect.top) / rect.height)), c: round(clamp((event.clientX - rect.left) / rect.width) * maxChroma) });
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = SQUARE_KEYS[event.key];
    if (disabled || !step) return;
    event.preventDefault();
    const scale = event.shiftKey ? 10 : 1;
    onChange({ ...value, l: round(clamp(value.l + step[0] * scale)), c: round(clamp(value.c + step[1] * scale, maxChroma)) });
  };
  return (
    <div ref={area} className={`relative h-[136px] w-full touch-none overflow-hidden rounded-card ${disabled ? "cursor-not-allowed opacity-60" : "cursor-crosshair"}`}
      onPointerDown={(event) => { if (disabled) return; event.currentTarget.setPointerCapture?.(event.pointerId); fromPointer(event); }}
      onPointerMove={(event) => { if (!disabled && event.currentTarget.hasPointerCapture?.(event.pointerId)) fromPointer(event); }}>
      <canvas ref={canvas} width={64} height={48} aria-hidden="true" className="absolute inset-0 size-full" />
      <div role="slider" aria-label={label} tabIndex={disabled ? -1 : 0} aria-disabled={disabled || undefined}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value.l * 100)} aria-valuetext={`Lightness ${value.l.toFixed(2)}, chroma ${value.c.toFixed(3)}`}
        onKeyDown={onKeyDown}
        style={{ left: `${(value.c / maxChroma) * 100}%`, top: `${(1 - value.l) * 100}%`, background: oklchToHex(value) }}
        className="absolute size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,.45)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus" />
    </div>
  );
}

function Field({ title, reset, children }: { title: string; reset?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2.5">
      <h4 className="flex min-h-7 items-center gap-1 font-sans text-[14px] font-semibold text-ink">{title}{reset}</h4>
      {children}
    </div>
  );
}

function OklchPicker({ name, value, maxChroma, disabled, onChange, reset }: {
  name: string; value: Oklch; maxChroma: number; disabled: boolean; onChange: (value: Oklch) => void; reset?: ReactNode;
}) {
  return (
    <Field title={name} reset={reset}>
      <ColourSquare label={`${name} lightness and chroma`} value={value} maxChroma={maxChroma} disabled={disabled} onChange={onChange} />
      <HueStrip label={`${name} hue`} hue={value.h} disabled={disabled} onChange={(h) => onChange({ ...value, h })} />
    </Field>
  );
}

function ResetIcon({ label, onReset }: { label: string; onReset: () => void }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onReset} className="flex size-7 items-center justify-center text-ink-3 hover:text-ink">
      <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true"><path d="M3 8a5 5 0 1 0 1.6-3.7M3 2.5v2.5h2.5" /></svg>
    </button>
  );
}

function HazeField({ theme, derived, disabled, onChange }: { theme: ThemeSpec; derived: DerivedTheme; disabled: boolean; onChange: (haze: ThemeSpec["haze"]) => void }) {
  const sources: [HazeSource, string][] = [["accent", "From accent"], ["hand", "By hand"]];
  return (
    <Field title="Haze">
      <div className="flex items-center gap-3">
        <div role="group" aria-label="Haze colours" className="inline-flex rounded-button border border-rule bg-paper-2 p-0.5">
          {sources.map(([source, name]) => (
            <button key={source} type="button" aria-pressed={theme.haze.source === source} disabled={disabled}
              onClick={() => onChange({ ...theme.haze, source, ...(source === "hand" ? { back: derived.haze.back.h, front: derived.haze.front.h } : {}) })}
              className="min-h-9 rounded-button px-3 text-[13px] text-ink-3 enabled:hover:text-ink disabled:cursor-not-allowed aria-pressed:bg-raised aria-pressed:text-ink">
              {name}
            </button>
          ))}
        </div>
        {[derived.haze.back, derived.haze.front].map((band, index) => (
          <span key={index} aria-hidden="true" className="size-6 rounded-chip border border-rule" style={{ background: oklchToHex(band) }} />
        ))}
      </div>
      {theme.haze.source === "hand" && (
        <div className="flex flex-col gap-3 pt-1">
          <HueStrip label="Haze back hue" hue={theme.haze.back} disabled={disabled} onChange={(back) => onChange({ ...theme.haze, back })} />
          <HueStrip label="Haze front hue" hue={theme.haze.front} disabled={disabled} onChange={(front) => onChange({ ...theme.haze, front })} />
        </div>
      )}
    </Field>
  );
}

function Confirming({ question, action, variant, onConfirm, onCancel }: {
  question: string; action: string; variant: "primary" | "danger-fill"; onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <div role="group" aria-label={question} className="flex items-center gap-2">
      <span className="text-[13px] text-ink">{question}</span>
      <Btn variant={variant} onClick={onConfirm}>{action}</Btn>
      <Btn onClick={onCancel}>Cancel</Btn>
    </div>
  );
}

function Actions({ dirty, saved, onSave, onReset, onDelete }: { dirty: boolean; saved: boolean; onSave: () => void; onReset: () => void; onDelete: () => void }) {
  const [asking, setAsking] = useState<"reset" | "delete" | null>(null);
  if (asking === "reset") return <Confirming question="Reset?" action="Reset" variant="primary" onConfirm={() => { onReset(); setAsking(null); }} onCancel={() => setAsking(null)} />;
  if (asking === "delete") return <Confirming question="Delete?" action="Delete" variant="danger-fill" onConfirm={onDelete} onCancel={() => setAsking(null)} />;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Btn variant="primary" disabled={saved && !dirty} onClick={onSave}>{saved && !dirty ? "Saved" : "Save"}</Btn>
      <Btn disabled={!dirty} onClick={() => setAsking("reset")}>Reset</Btn>
      {saved && <Btn variant="danger" onClick={() => setAsking("delete")}>Delete</Btn>}
    </div>
  );
}

const PREVIEW_ROWS: [token: "live" | "ask" | "bad", title: string, age: string][] = [
  ["live", "Prompt for a different contract PDF", "12m"],
  ["ask", "Retry a flaky verify once", "1h"],
  ["bad", "Retry the board sync", "50m"],
];

function Preview({ derived }: { derived: DerivedTheme }) {
  const style = { ...derived.tokens, "--preview-rule": "color-mix(in srgb, var(--ink) 12%, transparent)" } as CSSProperties;
  return (
    <figure aria-label="Theme preview" style={style} className="grid min-h-[300px] grid-cols-[132px_minmax(0,1fr)] overflow-hidden rounded-card border border-[var(--preview-rule)]">
      <div aria-hidden="true" className="flex flex-col gap-1 bg-side p-2.5" style={{ backgroundImage: "var(--grain-side)" }}>
        <span className="mb-2 flex items-center gap-2 px-1.5 py-1 font-sans text-[13px] font-semibold text-ink"><span className="size-3 rounded-[3px] bg-accent" />holophyte</span>
        {["Overview", "Runs", "Tickets", "Findings"].map((item, index) => (
          <span key={item} className={`rounded-button px-2 py-1 text-[12px] ${index ? "text-ink-2" : "bg-selected text-ink"}`}>{item}</span>
        ))}
      </div>
      <div aria-hidden="true" className="relative isolate bg-canvas px-6 pt-6 pb-5" style={{ backgroundImage: "var(--grain)" }}>
        <div className="haze-band haze-fallback absolute inset-x-0 top-0 -z-10 h-[120px]" />
        <p className="flex items-baseline gap-2 text-ink"><span className="font-sans text-[34px] leading-none font-light text-accent">3</span><span className="text-[16px]">running</span></p>
        <p className="mt-2 flex items-center gap-1.5 text-[12px] text-ink-3"><span className="text-live">✓</span>Nothing needs you</p>
        <div className="mt-4">
          {PREVIEW_ROWS.map(([token, title, age]) => (
            <div key={title} className="flex items-center gap-2.5 border-t border-[var(--preview-rule)] py-2 text-[12.5px]">
              <span className="size-2.5 rounded-full border-2" style={{ borderColor: `var(--${token})` }} />
              <span className="min-w-0 flex-1 truncate text-ink">{title}</span>
              <span className="font-mono text-[11px] text-ink-3">{age}</span>
            </div>
          ))}
        </div>
        <div className="mt-4 flex gap-2">
          <span className="rounded-button bg-accent px-3 py-1.5 text-[12px] font-medium" style={{ color: buttonInk(derived) }}>Approve merge</span>
          <span className="rounded-button border border-[var(--preview-rule)] px-3 py-1.5 text-[12px] text-ink">Answer</span>
        </div>
      </div>
    </figure>
  );
}

const ratioText = (ratio: number) => (Math.floor(ratio * 10) / 10).toFixed(1);

function ContrastFlags({ derived }: { derived: DerivedTheme }) {
  return (
    <ul aria-label="Contrast" className="flex flex-wrap gap-2">
      {contrastChecks(derived).map((check) => {
        const fails = check.ratio < check.bar;
        const tip = `${check.name} ${ratioText(check.ratio)}:1${fails ? ` · under ${check.bar}` : ""}`;
        return (
          <li key={check.name} aria-label={tip} title={tip} data-fails={fails || undefined}
            className={`flex items-center gap-1.5 rounded-chip border px-2 py-1 font-mono text-[12px] ${fails ? "border-bad text-bad" : "border-rule-2 bg-paper-2 text-ink-3"}`}>
            <span aria-hidden="true" className="size-3 rounded-[3px] border border-rule" style={{ background: check.colour }} />
            <span aria-hidden="true" className={fails ? "text-bad" : "text-live"}>{fails ? "!" : "✓"}</span>
            {ratioText(check.ratio)}
          </li>
        );
      })}
    </ul>
  );
}

function Editor({ initial, saved, readOnly, onSubject }: { initial: CustomTheme | ThemeSpec; saved: CustomTheme | null; readOnly: boolean; onSubject: (subject: EditorSubject | null) => void }) {
  const { saveCustomTheme, deleteCustomTheme } = useCustomThemes();
  const [theme, setTheme] = useState(initial);
  const baseline = saved ?? initial;
  const derived = deriveTheme(theme);
  const dirty = JSON.stringify(theme) !== JSON.stringify(baseline);
  const edit = (change: Partial<ThemeSpec>) => setTheme((current) => ({ ...current, ...change }));
  const origin = "origin" in theme ? theme.origin : theme.accent;
  const accentMoved = !readOnly && JSON.stringify(theme.accent) !== JSON.stringify(origin);
  const save = () => {
    const stored = theme as CustomTheme;
    saveCustomTheme(stored);
    if (!saved) onSubject({ mode: derived.mode, pick: stored.id });
  };
  return (
    <section aria-label="Theme editor" className="mt-2 grid gap-8 border-t border-rule-2 pt-6 min-[1100px]:grid-cols-[330px_minmax(0,1fr)]">
      <div className="flex flex-col gap-6">
        <input type="text" aria-label="Theme name" value={theme.name} disabled={readOnly} onChange={(event) => edit({ name: event.currentTarget.value })}
          className="min-h-10 rounded-button border border-rule bg-paper px-3 text-[15px] text-ink disabled:cursor-not-allowed disabled:text-ink-2" />
        <OklchPicker name="Ground" value={theme.ground} maxChroma={GROUND_CHROMA} disabled={readOnly} onChange={(ground) => edit({ ground })} />
        <OklchPicker name="Accent" value={theme.accent} maxChroma={ACCENT_CHROMA} disabled={readOnly} onChange={(accent) => edit({ accent })}
          reset={accentMoved && <ResetIcon label="Reset accent" onReset={() => edit({ accent: origin })} />} />
        <HazeField theme={theme} derived={derived} disabled={readOnly} onChange={(haze) => edit({ haze })} />
        <Field title="Grain">
          <div className="flex items-center gap-4">
            <input type="range" aria-label="Theme grain" min={GRAIN.min} max={GRAIN.max} step={GRAIN.step} value={theme.grain} disabled={readOnly}
              onChange={(event) => edit({ grain: Number(event.currentTarget.value) })} className="h-8 min-w-0 flex-1 accent-act disabled:cursor-not-allowed" />
            <output aria-label="Theme grain value" className="min-w-[44px] text-right font-mono text-[12px] text-ink-3">{theme.grain}%</output>
          </div>
        </Field>
        {readOnly ? (
          <div className="flex flex-wrap items-center gap-3">
            <Btn variant="primary" onClick={() => onSubject({ draft: copyOf(theme) })}>Edit a copy</Btn>
            <span className="text-[13px] text-ink-3">Built-in themes are read-only.</span>
          </div>
        ) : (
          <Actions dirty={dirty} saved={saved !== null} onSave={save} onReset={() => setTheme(baseline)}
            onDelete={() => { deleteCustomTheme(saved!.id); onSubject(null); }} />
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-4">
        <Preview derived={derived} />
        <ContrastFlags derived={derived} />
      </div>
    </section>
  );
}

export function ThemeEditor({ subject, onSubject }: { subject: EditorSubject; onSubject: (subject: EditorSubject | null) => void }) {
  const { customThemes } = useCustomThemes();
  const shown = subjectTheme(subject, customThemes);
  if (!shown) return null;
  const key = "draft" in subject ? subject.draft.id : `${subject.mode}:${subject.pick}`;
  const readOnly = !("draft" in subject) && !isCustom(subject.pick);
  return <Editor key={key} initial={shown.theme} saved={shown.saved} readOnly={readOnly} onSubject={onSubject} />;
}
