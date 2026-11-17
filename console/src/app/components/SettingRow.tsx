import { useEffect, useId, useState } from "react";
import { Link } from "@tanstack/react-router";
import { bind, PATH_LIST_KEYS, PATH_MATCH_DEBOUNCE_MS, UNBOUND_NOTE, type ConfigValues, type PatchValue } from "../../lib/config";
import type { Status } from "../../lib/contract.gen";
import { usePathMatches, useTrackedTree } from "../queries";
import { PathTree, PathViewSwitch, readPathView, writePathView } from "./PathTree";
import type { SettingsField } from "../settings";

function PathMatches({ base, settingKey, label, entries }: { base: string; settingKey: string; label: string; entries: string[] }) {
  const listed = JSON.stringify(entries);
  const [asked, setAsked] = useState(entries);
  useEffect(() => {
    const timer = setTimeout(() => setAsked(JSON.parse(listed) as string[]), PATH_MATCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [listed]);
  const { data } = usePathMatches(base, settingKey, asked);
  if (!data || entries.length === 0) return null;
  return (
    <ul aria-label={`${label} matches`} className="flex w-full min-w-0 flex-col gap-1 font-mono text-[12px]">
      {data.matches.map((match, index) => <li key={index} className={`break-words ${match.count === 0 ? "text-bad" : "text-ink-3"}`}>
        {match.entry} · {match.count === 0 ? "matches nothing" : `${match.count} ${match.count === 1 ? "file" : "files"}`}
      </li>)}
    </ul>
  );
}

const AUTOMATIC_SWATCH = "conic-gradient(var(--p-violet) 0 25%, var(--p-teal) 0 50%, var(--p-green) 0 75%, var(--p-blue) 0)";
const FIELD_CLASS = "w-full rounded-[7px] bg-raised px-3 py-2 font-mono text-[13px] text-ink shadow-[inset_0_0_0_1px_var(--hair-2)]";

function shown(field: SettingsField, value: PatchValue): string {
  if (field.kind === "select") return field.options.find((option) => option.value === value)?.label ?? String(value);
  if (field.kind === "boolean") return value === true ? "On" : "Off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
  if (value === "") return field.kind === "text" && field.seat ? "the default route" : "empty";
  return String(value);
}

export function SettingRow({ field, values, value: draft, onChange, base, project, host, applies, error, status, disabled = false }: {
  field: SettingsField;
  base: string;
  values: ConfigValues | null;
  value?: PatchValue;
  onChange: (value: PatchValue, original: PatchValue) => void;
  project: string;
  host: string;
  applies: string;
  error?: string;
  status?: Status;
  disabled?: boolean;
}) {
  const id = useId();
  const [lines, setLines] = useState<string | null>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const bound = bind(field, values);
  const original = bound.state === "bound" ? bound.value : field.default;
  const value = draft ?? original;
  const settingKey = `${field.table}.${field.key}`;
  const treeable = settingKey === "merge.ui_paths" && bound.state !== "unbound";
  const { data: tree } = useTrackedTree(base, treeable);
  const [view, setView] = useState(readPathView);
  const showTree = treeable && tree != null && view === "tree" && Array.isArray(value);
  const changed = draft !== undefined;
  const seat = field.table === "agents" && ["implementer", "reviewer", "adjudicator"].includes(field.key) ? field.key : null;
  const route = seat ? status?.active_routes?.[seat] : undefined;
  const label = seat ? status?.route_labels?.[seat] : undefined;
  const routeText = route?.down ? "down" : route?.fallback ? `on its fallback ${route.fallback}` : typeof label === "string" ? `runs ${label}` : null;
  const defaulted = bound.state === "absent" && !changed && !(field.kind === "text" && field.seat);
  const swatches = field.kind === "select" && field.options.some((option) => option.swatch);
  const stepClass = "grid size-7 cursor-pointer place-items-center rounded-[5px] text-ink-3 hover:bg-hover hover:text-ink disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent";
  return (
    <div role="group" aria-labelledby={`${id}-label`} data-setting={settingKey}
      onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false); }}
      className="flex min-w-0 flex-col gap-4 border-b border-hair py-[18px] first:border-t min-[860px]:grid min-[860px]:grid-cols-[minmax(0,1fr)_300px] min-[860px]:items-start min-[860px]:gap-7">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2">
          <h3 id={`${id}-label`} className="font-sans text-[14px] font-semibold">{field.label}</h3>
          <span data-setting-key style={{ opacity: hovered || focused ? 1 : 0 }} className="font-mono text-[11.5px] text-ink-3 transition-opacity duration-150">{settingKey}</span>
        </div>
        {field.help && <p className="mt-[5px] max-w-[46ch] font-sans text-[13px] leading-normal text-ink-2">{field.help.replace("{host}", host)}</p>}
        {changed && <p className="mt-2 flex items-center gap-1.5 font-sans text-[12.5px] text-ink-3"><span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-accent" />{`${shown(field, original)} → ${shown(field, value)} · applies at the ${applies}`}</p>}
        {error && <p role="alert" className="mt-2 font-mono text-[12px] text-bad">{error}</p>}
      </div>
      <div className="flex w-full min-w-0 flex-col items-start gap-2">
        {treeable && tree != null && <PathViewSwitch label={field.label} view={view} onPick={(next) => {
          setView(next);
          writePathView(next);
        }} />}
        {bound.state === "unbound" ? <div className="w-full min-w-0">
          <pre aria-label={field.label} className={`${FIELD_CLASS} whitespace-pre-wrap break-words text-[12px] text-ink-2`}>{JSON.stringify(bound.value, null, 2)}</pre>
          <Link to="/settings/$tab" params={{ tab: "toml" }} search={{ project }} className="mt-2 block font-mono text-[12px] text-act underline">{UNBOUND_NOTE}</Link>
        </div>
          : swatches && field.kind === "select" ? <div className="flex flex-wrap items-center gap-3 py-0.5">
            {field.options.map((option) => <button key={option.value} type="button" aria-label={option.label} aria-pressed={value === option.value} onClick={() => onChange(option.value, original)}
              style={{ background: option.swatch ? `var(${option.swatch})` : AUTOMATIC_SWATCH }}
              className={`group relative size-6 cursor-pointer rounded-full transition-shadow duration-150 ${value === option.value ? "shadow-[0_0_0_2px_var(--canvas),0_0_0_4px_var(--accent)]" : "shadow-[inset_0_0_0_1px_var(--hair-2)]"}`}>
              <span aria-hidden="true" className="pointer-events-none absolute bottom-[calc(100%+8px)] left-1/2 z-10 hidden -translate-x-1/2 whitespace-nowrap rounded-md bg-ink px-2 py-1 font-sans text-[12px] font-medium leading-[1.2] text-canvas group-hover:block group-focus-visible:block">{option.label}</span>
            </button>)}
          </div>
          : field.kind === "select" ? <div className="inline-flex max-w-full flex-wrap rounded-[7px] bg-[color:light-dark(var(--side),var(--raised))] p-0.5 shadow-[inset_0_0_0_1px_var(--hair)]">
            {field.options.map((option) => <button key={option.value} type="button" aria-pressed={value === option.value} onClick={() => onChange(option.value, original)}
              className={`inline-flex h-7 cursor-pointer items-center rounded-[5px] px-3 font-sans text-[12.5px] font-medium leading-none transition-colors duration-150 ${value === option.value ? "bg-[color:light-dark(var(--raised),var(--selected))] text-ink shadow-[0_1px_2px_rgba(29,24,43,.12)]" : "text-ink-3 hover:text-ink"}`}>
              {option.label}
            </button>)}
          </div> : field.kind === "number" ? <div className="inline-flex items-center rounded-[7px] bg-raised p-0.5 shadow-[inset_0_0_0_1px_var(--hair)]">
            <button type="button" aria-label={field.key === "workers" ? "One fewer worker" : `Decrease ${field.label.toLowerCase()}`} disabled={typeof value !== "number" || value <= field.min} onClick={() => onChange(Number(value) - 1, original)} className={stepClass}><StepIcon d="M4 8h8" /></button>
            <output aria-label={field.label} className="min-w-[30px] text-center font-mono text-[13px] tabular-nums">{String(value)}</output>
            <button type="button" aria-label={field.key === "workers" ? "One more worker" : `Increase ${field.label.toLowerCase()}`} onClick={() => onChange(Number(value) + 1, original)} className={stepClass}><StepIcon d="M8 4v8M4 8h8" /></button>
          </div> : field.kind === "boolean" ? <button type="button" role="switch" aria-label={field.label} aria-checked={value === true} disabled={disabled} onClick={() => onChange(!value, original)}
            className={`inline-flex h-8 cursor-pointer items-center rounded-[7px] px-3 font-sans text-[12.5px] font-medium shadow-[inset_0_0_0_1px_var(--hair)] disabled:cursor-default disabled:opacity-40 ${value === true ? "bg-[color:light-dark(var(--raised),var(--selected))] text-ink" : "bg-raised text-ink-3 hover:text-ink"}`}>{value === true ? "On" : "Off"}</button>
            : showTree ? <PathTree tree={tree} value={value} label={field.label} onChange={(next) => onChange(next, original)} />
            : field.kind === "lines" ? <textarea aria-label={field.label} rows={3} value={lines ?? (Array.isArray(value) ? value.join("\n") : "")} onBlur={() => setLines(null)} onChange={(event) => {
              setLines(event.target.value);
              onChange(event.target.value.split("\n").filter((line) => line.trim() !== ""), original);
            }} className={FIELD_CLASS} />
            : <input type="text" aria-label={field.label} value={String(value)} placeholder={field.seat ? "the default route" : undefined} onChange={(event) => onChange(event.target.value, original)} className={FIELD_CLASS} />}
        {field.kind === "lines" && !showTree && bound.state !== "unbound" && PATH_LIST_KEYS.has(settingKey) && Array.isArray(value) &&
          <PathMatches base={base} settingKey={settingKey} label={field.label} entries={value} />}
        {(routeText || defaulted) && <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[12px]">
          {routeText && <span className={route?.down ? "text-bad" : route?.fallback ? "text-ask-ink" : "text-live"}>{routeText}</span>}
          {defaulted && <span className="text-ink-3">default</span>}
        </div>}
      </div>
    </div>
  );
}

function StepIcon({ d }: { d: string }) {
  return <svg aria-hidden="true" viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round"><path d={d} /></svg>;
}
