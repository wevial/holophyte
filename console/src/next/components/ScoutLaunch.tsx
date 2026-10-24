import { useEffect, useRef, type ReactNode } from "react";
import type { ScoutRoute, ScoutRow } from "../../lib/contract.gen";
import type { useAction } from "../actions";
import { aboutWords, defaultPicks, sentence, useScoutRoutes, wholeIn, type ScoutPicks, type ScoutsSearch } from "../scouts";
import { Btn } from "./Btn";
import { FoldChevron } from "./icons";
import { INPUT } from "./TicketFields";

type Kind = ScoutRow["kind"];

const KINDS: { id: Kind; label: string }[] = [
  { id: "bug", label: "Bug" },
  { id: "research", label: "Research" },
  { id: "recon", label: "Recon" },
];

export interface LaunchProject {
  base: string;
  name: string;
}

/** What the reader has typed and picked; `picks` hold for the project at
 *  `picks.base` only, so another project starts from its own defaults.
 *  `about` is the launch's `subject`. */
interface ScoutDraft {
  ask: string;
  kind: Kind;
  base: string | null;
  disclosed: boolean;
  picks: (ScoutPicks & { base: string }) | null;
  about: string | null;
}

export const EMPTY_DRAFT: ScoutDraft = { ask: "", kind: "research", base: null, disclosed: false, picks: null, about: null };

/** The form a Scout button opened: the search's project when `projects`
 *  offers it, with its subject; else the blank form's project and none. */
export function prefilledDraft(search: ScoutsSearch, projects: LaunchProject[]): ScoutDraft {
  const base = projects.find((project) => project.name === search.new)?.base ?? null;
  const about = base != null && search.about != null && aboutWords(search.about) != null ? search.about : null;
  return { ...EMPTY_DRAFT, ask: search.ask ?? "", kind: search.kind ?? "research", base, about };
}

function AboutLine({ about, onRemove }: { about: string; onRemove: () => void }) {
  const words = aboutWords(about);
  if (words == null) return null;
  return (
    <div className="mb-2 flex items-center gap-2">
      <p data-about className="m-0 min-w-0 font-mono text-[12.5px] [overflow-wrap:anywhere] text-ink-2">About {words}</p>
      <button
        type="button"
        aria-label={`Remove ${words}`}
        title={`Remove ${words}`}
        onClick={onRemove}
        className="inline-flex size-6 shrink-0 cursor-pointer items-center justify-center font-mono text-[14px] leading-none text-ink-3 hover:text-ink"
      >
        ×
      </button>
    </div>
  );
}

const LABEL = "font-sans text-[14px] font-semibold text-ink";
const PRESS =
  "min-h-9 cursor-pointer border-l border-ink-3 px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.1em] text-ink-2 first:border-l-0 aria-pressed:bg-ink aria-pressed:text-paper";

function Pressed<T extends string>({ label, options, picked, onPick }: {
  label: string;
  options: { id: T; label: string; title?: string }[];
  picked: T | null;
  onPick: (id: T) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className={LABEL}>{label}</span>
      <div role="group" aria-label={label} className="inline-flex flex-wrap self-start border border-ink-3">
        {options.map((option) => (
          <button key={option.id} type="button" title={option.title} aria-pressed={picked === option.id} onClick={() => onPick(option.id)} className={PRESS}>
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

const routeOptions = (routes: ScoutRoute[]) => routes.map((route) => ({ id: route.name, label: route.name, title: `${route.harness} · ${route.model}` }));

function NumberField({ id, label, value, least, most, onChange }: {
  id: string;
  label: string;
  value: string;
  least: number;
  most?: number;
  onChange: (value: string) => void;
}) {
  return (
    <label htmlFor={id} className="flex flex-col">
      <span className={LABEL}>{label}</span>
      <input id={id} type="number" inputMode="numeric" min={least} max={most} step={1} value={value} onChange={(event) => onChange(event.target.value)} className={`${INPUT} max-w-24 font-mono`} />
    </label>
  );
}

function Band({ children }: { children: ReactNode }) {
  return <div className="border-b border-rule-2 p-5 last:border-b-0 max-[720px]:p-4">{children}</div>;
}

/** The New scout form: the ask, its kind and project, and one disclosure
 *  for the routes, swarm size and minutes, posted to the picked project. */
export function ScoutLaunch({ projects, action, draft, onDraft, onCancel, onLaunched }: {
  projects: LaunchProject[];
  /** Held by the page, so a launch still pending when the form closes
   *  keeps its Scout button busy when the form opens again. */
  action: ReturnType<typeof useAction>;
  draft: ScoutDraft;
  onDraft: (draft: ScoutDraft) => void;
  onCancel: () => void;
  onLaunched: () => void;
}) {
  const askRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const ask = askRef.current;
    ask?.focus();
    ask?.setSelectionRange(ask.value.length, ask.value.length);
  }, []);
  const project = projects.find((each) => each.base === draft.base) ?? projects[0]!;
  const { data: routes } = useScoutRoutes(project.base);
  const leads = routes?.leads ?? [];
  const helperRoutes = routes?.helpers ?? [];
  const picks: ScoutPicks | null = draft.picks?.base === project.base ? draft.picks : routes ? defaultPicks(routes) : null;
  const turns = routes?.defaults.helper_turns ?? 1;
  const swarm = picks ? wholeIn(picks.swarm, 1, turns) : null;
  const minutes = picks ? wholeIn(picks.minutes, 1) : null;
  const missing = routes === undefined ? null : leads.length === 0 ? "lead" : helperRoutes.length === 0 ? "helper" : null;
  const ready = draft.ask.trim() !== "" && picks?.lead != null && picks.helper != null && swarm != null && minutes != null && missing == null;
  const refusal = action.data && !action.data.ok ? action.data.detail : null;
  const set = (change: Partial<ScoutDraft>) => onDraft({ ...draft, ...change });
  const pick = (change: Partial<ScoutPicks>) => picks && set({ picks: { ...picks, ...change, base: project.base } });
  const launch = () => {
    if (!ready) return;
    const body = { ask: draft.ask.trim(), kind: draft.kind, lead: picks.lead, helper: picks.helper, swarm, minutes, ...(draft.about == null ? {} : { subject: draft.about }) };
    action.mutate(
      { label: "Scout", base: project.base, route: "/actions/scout", body, said: sentence },
      { onSuccess: (result) => result.ok && onLaunched() },
    );
  };
  const summary = picks ? `${picks.lead ?? "–"} · ${picks.helper ?? "–"} ×${picks.swarm} · ${picks.minutes} min` : "reading routes…";
  return (
    <form
      aria-label="New scout"
      onSubmit={(event) => {
        event.preventDefault();
        launch();
      }}
      className="mb-4 border border-rule bg-paper"
    >
      <Band>
        {draft.about != null && <AboutLine about={draft.about} onRemove={() => set({ about: null })} />}
        <label htmlFor="scout-ask" className="flex flex-col">
          <span className={LABEL}>Ask</span>
          <textarea id="scout-ask" ref={askRef} rows={2} value={draft.ask} onChange={(event) => set({ ask: event.target.value })} className={`${INPUT} resize-y font-sans`} />
        </label>
      </Band>
      <Band>
        <div className="flex flex-wrap items-start gap-x-8 gap-y-5">
          <Pressed label="Kind" options={KINDS} picked={draft.kind} onPick={(kind) => set({ kind })} />
          <label htmlFor="scout-project" className="flex w-full max-w-[260px] flex-col">
            <span className={LABEL}>Project</span>
            <select id="scout-project" value={project.base} onChange={(event) => set({ base: event.target.value, picks: null, about: null })} className={`${INPUT} font-sans`}>
              {projects.map((each) => <option key={each.base} value={each.base}>{each.name}</option>)}
            </select>
          </label>
        </div>
      </Band>
      <Band>
        <button
          type="button"
          aria-expanded={draft.disclosed}
          onClick={() => set({ disclosed: !draft.disclosed })}
          className="group inline-flex min-h-9 cursor-pointer items-center gap-2 font-mono text-[13px] text-ink"
        >
          <FoldChevron open={draft.disclosed} />
          {summary}
        </button>
        {draft.disclosed && picks && (
          <div className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] items-start gap-x-6 gap-y-5">
            <Pressed label="Lead" options={routeOptions(leads)} picked={picks.lead} onPick={(lead) => pick({ lead })} />
            <Pressed label="Helpers" options={routeOptions(helperRoutes)} picked={picks.helper} onPick={(helper) => pick({ helper })} />
            <NumberField id="scout-swarm" label="At once" value={picks.swarm} least={1} most={turns} onChange={(value) => pick({ swarm: value })} />
            <NumberField id="scout-minutes" label="Minutes" value={picks.minutes} least={1} onChange={(value) => pick({ minutes: value })} />
          </div>
        )}
      </Band>
      <Band>
        <div className="flex flex-wrap justify-end gap-3">
          <Btn onClick={onCancel}>Cancel</Btn>
          <Btn variant="primary" busy={action.isPending} busyLabel="Scouting…" disabled={!ready} onClick={launch}>Scout</Btn>
        </div>
        {missing && <p className="mt-3 text-right font-mono text-[12.5px] text-bad">No {missing} route on {project.name} passed its probe.</p>}
        {refusal && <p role="alert" className="mt-3 text-right font-mono text-[12.5px] [overflow-wrap:anywhere] text-bad">{refusal}</p>}
      </Band>
    </form>
  );
}
