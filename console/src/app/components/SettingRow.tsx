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
  const buttonClass = "min-h-9 cursor-pointer px-3 font-mono text-[12px] disabled:cursor-default disabled:opacity-40";
  return (
    <div role="group" aria-labelledby={`${id}-label`} data-setting={`${field.table}.${field.key}`} className={`flex min-w-0 flex-col gap-4 border-b border-rule-2 border-l-2 px-4 py-5 min-[860px]:flex-row min-[860px]:items-start min-[860px]:justify-between ${changed ? "border-l-act bg-paper-2" : "border-l-transparent"}`}>
      <div className="min-w-0">
        <h3 id={`${id}-label`} className="font-sans text-[15px] font-semibold">{field.label}</h3>
        <p className="mt-1 font-mono text-[12px] text-ink-3">{field.table}.{field.key}</p>
        {field.help && <p className="mt-2 max-w-[46ch] font-read text-[14px] leading-relaxed text-ink-2">{field.help.replace("{host}", host)}</p>}
        {changed && <p className="mt-3 flex items-center gap-2 font-mono text-[12px] text-ink-3"><svg aria-hidden="true" viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.5} className="shrink-0 text-ask-ink"><path d="M13 6a5 5 0 0 0-8.5-3.5L2 5m0-4v4h4M3 10a5 5 0 0 0 8.5 3.5L14 11m0 4v-4h-4" /></svg>applies at the {applies}</p>}
        {error && <p role="alert" className="mt-3 font-mono text-[12px] text-bad">{error}</p>}
      </div>
      <div className="flex w-full min-w-0 flex-col items-start gap-2 min-[860px]:w-80 min-[860px]:shrink-0">
        {treeable && tree != null && <PathViewSwitch label={field.label} view={view} onPick={(next) => {
          setView(next);
          writePathView(next);
        }} />}
        {bound.state === "unbound" ? <div className="w-full min-w-0">
          <pre aria-label={field.label} className="w-full whitespace-pre-wrap break-words border border-rule bg-paper px-3 py-2 font-mono text-[12px] text-ink-2">{JSON.stringify(bound.value, null, 2)}</pre>
          <Link to="/settings/$tab" params={{ tab: "toml" }} search={{ project }} className="mt-2 block font-mono text-[12px] text-act underline">{UNBOUND_NOTE}</Link>
        </div>
          : field.kind === "select" ? <div className="inline-flex max-w-full flex-wrap border border-rule bg-paper [&>*+*]:border-l [&>*+*]:border-rule">
            {field.options.map((option) => <button key={option.value} type="button" aria-pressed={value === option.value} onClick={() => onChange(option.value, original)} className={`${buttonClass} inline-flex items-center gap-2 ${value === option.value ? "bg-act-wash text-act" : "text-ink-2 hover:text-ink"}`}>
              {option.swatch && <span data-swatch aria-hidden="true" className="size-2.5 shrink-0" style={{ background: `var(${option.swatch})` }} />}
              {option.label}
            </button>)}
          </div> : field.kind === "number" ? <div className="inline-flex items-center border border-rule bg-paper">
            <button type="button" aria-label={field.key === "workers" ? "One fewer worker" : `Decrease ${field.label.toLowerCase()}`} disabled={typeof value !== "number" || value <= field.min} onClick={() => onChange(Number(value) - 1, original)} className={`${buttonClass} text-ink-2`}>−</button>
            <output aria-label={field.label} className="min-w-10 border-x border-rule px-3 py-2 text-center font-mono text-[13px]">{String(value)}</output>
            <button type="button" aria-label={field.key === "workers" ? "One more worker" : `Increase ${field.label.toLowerCase()}`} onClick={() => onChange(Number(value) + 1, original)} className={`${buttonClass} text-ink-2`}>+</button>
          </div> : field.kind === "boolean" ? <button type="button" role="switch" aria-label={field.label} aria-checked={value === true} disabled={disabled} onClick={() => onChange(!value, original)} className={`${buttonClass} border border-rule ${value === true ? "bg-act-wash text-act" : "bg-paper text-ink-2"}`}>{value === true ? "On" : "Off"}</button>
            : showTree ? <PathTree tree={tree} value={value} label={field.label} onChange={(next) => onChange(next, original)} />
            : field.kind === "lines" ? <textarea aria-label={field.label} rows={3} value={lines ?? (Array.isArray(value) ? value.join("\n") : "")} onBlur={() => setLines(null)} onChange={(event) => {
              setLines(event.target.value);
              onChange(event.target.value.split("\n").filter((line) => line.trim() !== ""), original);
            }} className="w-full border border-rule bg-paper px-3 py-2 font-mono text-[13px] text-ink" />
            : <input type="text" aria-label={field.label} value={String(value)} placeholder={field.seat ? "the default route" : undefined} onChange={(event) => onChange(event.target.value, original)} className="w-full border border-rule bg-paper px-3 py-2 font-mono text-[13px] text-ink" />}
        {field.kind === "lines" && !showTree && bound.state !== "unbound" && PATH_LIST_KEYS.has(settingKey) && Array.isArray(value) &&
          <PathMatches base={base} settingKey={settingKey} label={field.label} entries={value} />}
        {(routeText || defaulted || changed) && <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[12px]">
          {routeText && <span className={route?.down ? "text-bad" : route?.fallback ? "text-ask-ink" : "text-live"}>{routeText}</span>}
          {defaulted && <span className="text-ink-3">default</span>}
          {changed && <span className="text-act">was {String(original)}</span>}
        </div>}
      </div>
    </div>
  );
}
