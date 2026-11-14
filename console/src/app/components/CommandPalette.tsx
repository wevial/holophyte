import { createListbox, createPopup, definePrimitive, popupBackdrop, popupContent, popupState, popupTitle, selects } from "@ui-sdk/core";
import { dialogPreset } from "@ui-sdk/core/dialog";
import { createParts, usePart, usePrimitive } from "@ui-sdk/react/primitive";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import { useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { ACTIONS_OFF, ROUTES } from "../../lib/actions";
import { projectName } from "../../lib/derive";
import { byDaemon, hostName } from "../../lib/hosts";
import type { Theme } from "../../lib/theme";
import { useAction } from "../actions";
import { useTheme } from "../appearance";
import {
  CHIPS,
  findingEntry,
  isTicketKey,
  marks,
  PAGES,
  pageEntry,
  projectEntry,
  pushRecent,
  readRecent,
  runEntry,
  scoutEntry,
  takeChip,
  ticketEntry,
  type Chip,
  type PaletteAction,
  type PaletteEntry,
  type PaletteGlyph,
  type PaletteGroup,
} from "../palette";
import { storeScope } from "../projects";
import { useSearchAll } from "../queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";
import { Btn } from "./Btn";
import { AllProjectsIcon, FindingsIcon, FolderIcon } from "./icons";
import { comboInput, virtualOption, type ComboApi } from "./ProjectSwitcher";
import { StateDot } from "./StateDot";

const SEARCH_WAIT_MS = 150;

type PaletteOptions = {
  defaultOpen?: boolean;
  items?: { value: string; label: string; disabled?: boolean }[];
  onChoose?: (key: string) => void;
  onQuery?: (query: string) => void;
  onEmptyBackspace?: () => void;
  onOpenChangeComplete?: (open: boolean) => void;
};

const palette = definePrimitive({
  name: "command-palette",
  options: {
    defaultOpen: "boolean",
    items: "value",
    onChoose: "callback",
    onQuery: "callback",
    onEmptyBackspace: "callback",
    onOpenChangeComplete: "callback",
  },
  state: (options: PaletteOptions) => ({ ...popupState(options), query: "", highlighted: null as string | null }),
  api: (context) => {
    const popup = createPopup(context, { preset: dialogPreset });
    const listbox = createListbox(context, { key: "highlighted", focus: "virtual", items: () => context.options().items ?? [] });
    const choose = (key: string | null) => {
      if (key == null || context.options().items?.find((item) => item.value === key)?.disabled) return;
      context.options().onChoose?.(key);
    };
    const api: ComboApi<ReturnType<typeof context.store.get>> & typeof popup & { replaceQuery(query: string): void } = {
      ...popup,
      listbox,
      choose,
      search(query: string) {
        context.store.set({ query });
        context.options().onQuery?.(query);
      },
      replaceQuery(query: string) {
        context.store.set({ query });
      },
      keydown(event: KeyboardEvent) {
        if (event.key === "Backspace" && context.store.get().query === "") {
          context.options().onEmptyBackspace?.();
          return;
        }
        if (event.key !== "Enter") return void listbox.keydown(event);
        event.preventDefault();
        choose(context.store.get().highlighted);
      },
    };
    return api;
  },
  slots: (slot) => ({
    root: slot({}),
    backdrop: popupBackdrop(slot),
    content: popupContent(slot),
    title: popupTitle(slot),
    input: comboInput(slot),
    list: slot({ role: "listbox", keys: { none: "The combobox's input handles them." }, attrs: (_state, { id }) => ({ id: `${id}-list` }) }),
    option: virtualOption(slot, (state, key) => key === state.highlighted, selects({ highlighted: ({ key }) => [key, null] })),
  }),
});

const { Backdrop, Content, Title, Input, List, Option } = createParts(palette, {
  backdrop: "div",
  content: "div",
  title: "h2",
  input: "input",
  list: "div",
  option: "div",
});

const THEME_NEXT: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };
const THEME_WORD: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

const ACTION_TITLES: Record<PaletteAction, string> = {
  "new-ticket": "New ticket",
  "launch-scout": "Launch scout",
  "switch-theme": "Switch theme",
  "run-sweep": "Run sweep",
};

const CONFIRMED = new Set<PaletteAction>(["switch-theme", "run-sweep"]);

function actionEntry(action: PaletteAction, meta: string, disabled?: string, query?: string): PaletteEntry {
  return {
    key: `action:${action}${query == null ? "" : `:${query}`}`,
    title: query == null ? ACTION_TITLES[action] : `${ACTION_TITLES[action]} "${query}"`,
    meta: disabled ?? meta,
    glyph: { icon: "action", label: "action" },
    go: { kind: "action", action, ...(query == null ? {} : { query }) },
    ...(disabled == null ? {} : { disabled }),
  };
}

function useActions(project: string | undefined) {
  const { hosts } = useConsolePeers();
  const { theme } = useTheme();
  const scoped = scopedHosts(hosts, project);
  const named = scoped.flatMap((host) => (host.status ? [projectName(host.status.project)] : []));
  const ticketProject = project ?? named[0];
  const launchProject = scoped.find((host) => host.status?.actions === true)?.status?.project;
  const daemons = byDaemon(scoped).flatMap((records) => (records[0]?.host_status ? [records[0]] : []));
  const sweeps = daemons.filter((daemon) => daemon.host_status?.actions === true);
  const sweepOff = daemons.length === 0 ? "No host daemon in this scope runs a sweep" : sweeps.length === 0 ? ACTIONS_OFF : undefined;
  const sweepOn = sweeps.map(hostName).join(" and ");
  const newTicket = (query?: string) =>
    actionEntry("new-ticket", ticketProject == null ? "" : `in ${ticketProject}`, ticketProject == null ? "No project answers yet" : undefined, query);
  return {
    newTicket,
    ticketProject,
    launchProject: launchProject == null ? undefined : projectName(launchProject),
    sweeps,
    nextTheme: THEME_NEXT[theme],
    all: [
      newTicket(),
      actionEntry("launch-scout", launchProject == null ? "" : `in ${projectName(launchProject)}`, launchProject == null ? ACTIONS_OFF : undefined),
      actionEntry("switch-theme", `${THEME_WORD[theme]} → ${THEME_WORD[THEME_NEXT[theme]]}`),
      actionEntry("run-sweep", `on ${sweepOn}`, sweepOff),
    ],
  };
}

const contains = (text: string, query: string) => text.toLowerCase().includes(query.trim().toLowerCase());

function useDebounced(text: string): string {
  const [settled, setSettled] = useState(text);
  useEffect(() => {
    const wait = setTimeout(() => setSettled(text), SEARCH_WAIT_MS);
    return () => clearTimeout(wait);
  }, [text]);
  return settled;
}

interface Listing {
  groups: PaletteGroup[];
  loading: boolean;
  empty: string | null;
}

function useListing(chip: Chip | null, text: string, project: string | undefined): Listing & { actions: ReturnType<typeof useActions> } {
  const { hosts } = useConsolePeers();
  const deps = useContext(PollDepsContext);
  const scoped = scopedHosts(hosts, project);
  const query = text.trim();
  const settled = useDebounced(query);
  const searching = chip !== ">" && query !== "";
  const found = useSearchAll(scoped, searching ? settled : "", deps);
  const actions = useActions(project);
  const nameOf = (host: (typeof scoped)[number]) => projectName(host.status?.project ?? host.project ?? host.label);
  if (chip == null && query === "") {
    const names = [...new Set(hosts.flatMap((host) => (host.project == null ? [] : [projectName(host.project)])))];
    const recent = readRecent();
    return {
      actions,
      loading: false,
      empty: null,
      groups: [
        ...(recent.length ? [{ name: "Recent", entries: recent }] : []),
        { name: "Jump to", entries: PAGES.map((page) => pageEntry(page)) },
        { name: "Switch project", entries: [null, ...names].map((name) => projectEntry(name, name === (project ?? null))) },
      ],
    };
  }
  const loading = searching && (settled !== query || found.pending);
  const tickets = found.tickets.map(({ host, hit }) => ticketEntry(nameOf(host), hit, host.base));
  const exact = isTicketKey(query) ? tickets.filter((entry) => entry.go.kind === "ticket" && entry.go.key.toLowerCase() === query.toLowerCase()) : [];
  const groups: PaletteGroup[] = [];
  if (chip == null || chip === "#") {
    groups.push({ name: "Go to", entries: exact });
    groups.push({ name: "Tickets", entries: tickets.filter((entry) => !exact.includes(entry)) });
  }
  if (chip == null || chip === "@") groups.push({ name: "Runs", entries: found.runs.map(({ host, hit }) => runEntry(nameOf(host), hit, host.base)) });
  if (chip == null) {
    groups.push({ name: "Scouts", entries: found.scouts.map(({ host, hit }) => scoutEntry(nameOf(host), hit, host.base)) });
    groups.push({ name: "Findings", entries: found.findings.map(({ host, hit }, index) => findingEntry(nameOf(host), hit, index, host.base)) });
    const searched = { "/tickets": tickets.length, "/runs": found.runs.length, "/scouts": found.scouts.length } as Record<string, number>;
    groups.push({
      name: "Pages",
      entries: [
        ...PAGES.filter((page) => contains(page.label, query)).map((page) => pageEntry(page)),
        ...PAGES.filter((page) => (searched[page.to] ?? 0) > 0).map((page) => pageEntry(page, query)),
      ],
    });
  }
  const named = chip == null || chip === ">" ? actions.all.filter((entry) => contains(entry.title, query)) : [];
  const shown = groups.filter((group) => group.entries.length > 0);
  const matched = shown.length > 0 || named.length > 0;
  if (chip == null || chip === ">") {
    const offer = query === "" ? [] : [actions.newTicket(query)];
    groups.push({ name: "Actions", entries: [...named, ...offer] });
  }
  const scopeWord = chip === "#" ? "tickets" : chip === "@" ? "runs" : null;
  return {
    actions,
    loading,
    empty: loading || matched ? null : query === "" && scopeWord ? `Type to search ${scopeWord}` : "No matches",
    groups: groups.filter((group) => group.entries.length > 0),
  };
}

function Marked({ text, query }: { text: string; query: string }) {
  return marks(text, query).map((span, index) =>
    span.match ? (
      <mark key={index} className="rounded-[3px] bg-act-wash px-0.5 text-ink">
        {span.text}
      </mark>
    ) : (
      <span key={index}>{span.text}</span>
    ),
  );
}

function PageIcon() {
  return (
    <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 2h5.5L12 4.5V14H4z" />
      <path d="M9.5 2v2.5H12" />
    </svg>
  );
}

function ActionIcon() {
  return (
    <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 1.5 3.5 9H8l-1 5.5L12.5 7H8z" />
    </svg>
  );
}

const ICONS: Record<Exclude<PaletteGlyph, { state: unknown }>["icon"], () => ReactNode> = {
  page: () => <PageIcon />,
  project: () => <FolderIcon />,
  "all-projects": () => <AllProjectsIcon />,
  finding: () => <FindingsIcon />,
  action: () => <ActionIcon />,
};

function Glyph({ glyph }: { glyph: PaletteGlyph }) {
  if ("state" in glyph) return <StateDot state={glyph.state} hideWord label={glyph.label} />;
  return (
    <span role="img" aria-label={glyph.label} className={`flex ${glyph.tone ?? "text-ink-2"}`}>
      {ICONS[glyph.icon]()}
    </span>
  );
}

const KBD = "inline-flex h-[22px] min-w-[22px] items-center justify-center rounded-[5px] border border-rule bg-paper px-1.5 font-mono text-[11px] leading-none text-ink-2";

function Hint({ keys, tip }: { keys: string; tip: string }) {
  return (
    <Tooltip delay={300}>
      <TooltipTrigger render={(trigger) => <kbd {...trigger} tabIndex={-1} aria-label={tip} className={KBD}>{keys}</kbd>} />
      <TooltipContent side="top" sideOffset={6} className="z-[60] rounded-[6px] border border-rule-2 bg-raised px-2.5 py-1 text-[12px] font-medium whitespace-nowrap text-ink shadow-card">
        {tip}
      </TooltipContent>
    </Tooltip>
  );
}

function Skeleton() {
  return (
    <div aria-hidden="true" data-skeleton className="flex flex-col gap-1 px-1.5 py-1">
      {[0, 1, 2].map((row) => (
        <div key={row} className="flex items-center gap-3 px-2.5 py-2">
          <span className="size-3.5 animate-pulse rounded-full bg-paper-2" />
          <span className="flex flex-1 flex-col gap-1.5">
            <span className="h-3 w-3/5 animate-pulse rounded bg-paper-2" />
            <span className="h-2.5 w-2/5 animate-pulse rounded bg-paper-2" />
          </span>
        </div>
      ))}
    </div>
  );
}

function Confirm({ entry, busy, refusal, onConfirm, onCancel }: { entry: PaletteEntry; busy: boolean; refusal: string | null; onConfirm: () => void; onCancel: () => void }) {
  const confirm = useRef<HTMLButtonElement>(null);
  useEffect(() => confirm.current?.focus(), []);
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="px-5 py-5">
      <p className="font-mono text-[11px] font-medium uppercase tracking-[0.1em] text-ink-3">Confirm</p>
      <h3 id={headingId} className="mt-2 text-[17px] font-semibold text-ink">{entry.title}</h3>
      <p className="mt-1 font-mono text-[12px] text-ink-2">{entry.meta}</p>
      {refusal && <p className="mt-3 font-mono text-[12px] text-bad">{refusal}</p>}
      <div className="mt-5 flex justify-end gap-2">
        <Btn onClick={onCancel}>Cancel</Btn>
        <Btn ref={confirm} variant="primary" busy={busy} busyLabel="Confirming…" onClick={onConfirm}>Confirm</Btn>
      </div>
    </section>
  );
}

/** The ⌘K palette, open from mount until it closes: then `onClosed` runs
 *  and focus goes back to `returnTo`. */
export function CommandPalette({ onClosed, returnTo }: { onClosed: () => void; returnTo: () => HTMLElement | null }) {
  const { project } = useSearch({ from: "__root__" });
  const navigate = useNavigate();
  const [chip, setChip] = useState<Chip | null>(null);
  const [text, setText] = useState("");
  const [confirming, setConfirming] = useState<PaletteEntry | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const { setTheme } = useTheme();
  const sweep = useAction();
  const swept = useRef(new Set<string>());
  const listing = useListing(chip, text, project);
  const { actions } = listing;
  const query = text.trim();
  const inputId = `${useId()}-input`;

  const keyed = useMemo(
    () => listing.groups.flatMap((group, at) => group.entries.map((entry) => ({ value: `${at}|${entry.key}`, entry }))),
    [listing.groups],
  );
  const byKey = new Map(keyed.map(({ value, entry }) => [value, entry]));
  const items = confirming ? [] : keyed.map(({ value, entry }) => ({ value, label: entry.title, disabled: entry.disabled != null }));

  const close = () => handle.api.close();

  const act = (entry: PaletteEntry, action: PaletteAction, query?: string) => {
    if (action === "new-ticket" && actions.ticketProject != null) {
      void navigate({ to: "/tickets/$project/new", params: { project: actions.ticketProject }, search: keepScope, state: query == null ? undefined : { draft: `# ${query}\n` } });
    } else if (action === "launch-scout" && actions.launchProject != null) {
      void navigate({ to: "/scouts", search: (previous: ScopeSearch) => ({ ...keepScope(previous), new: actions.launchProject }) });
    } else if (CONFIRMED.has(action)) {
      setRefusal(null);
      setConfirming(entry);
      return;
    }
    close();
  };

  const open = (entry: PaletteEntry) => {
    const go = entry.go;
    if (go.kind === "action") return act(entry, go.action, go.query);
    pushRecent(entry);
    if (go.kind === "ticket") void navigate({ to: "/tickets/$project/$key", params: { project: go.project, key: go.key }, search: keepScope });
    else if (go.kind === "run") void navigate({ to: "/runs/$project/$run", params: { project: go.project, run: String(go.run) }, search: keepScope });
    else if (go.kind === "scout") void navigate({ to: "/scouts/$project/$scout", params: { project: go.project, scout: String(go.scout) }, search: keepScope });
    else if (go.kind === "project") {
      storeScope(go.name);
      void navigate({ to: ".", search: (previous: ScopeSearch) => ({ ...previous, project: go.name ?? undefined }) });
    } else if (go.to === "/settings/appearance") void navigate({ to: "/settings/$tab", params: { tab: "appearance" }, search: keepScope });
    else if (go.q != null && (go.to === "/runs" || go.to === "/tickets" || go.to === "/scouts")) {
      const q = go.q;
      void navigate({ to: go.to, search: (previous: ScopeSearch) => ({ ...keepScope(previous), q }) });
    } else void navigate({ to: go.to, search: keepScope });
    close();
  };

  const confirm = async () => {
    if (!confirming || confirming.go.kind !== "action") return;
    if (confirming.go.action === "switch-theme") {
      setTheme(actions.nextTheme);
      return close();
    }
    for (const daemon of actions.sweeps.filter((each) => !swept.current.has(each.base))) {
      const result = await sweep
        .mutateAsync({ label: "Run sweep", base: daemon.base, route: ROUTES["Run sweep"]!, body: {}, said: `Asked ${hostName(daemon)} to sweep.` })
        .catch((failure: unknown) => ({ ok: false, detail: failure instanceof Error ? failure.message : String(failure) }));
      if (!result.ok) return setRefusal(result.detail);
      swept.current.add(daemon.base);
    }
    close();
  };

  const cancel = () => {
    setConfirming(null);
    document.getElementById(inputId)?.focus();
  };

  const handle = usePrimitive(palette, {
    defaultOpen: true,
    items,
    onChoose: (key) => {
      const entry = byKey.get(key);
      if (entry) open(entry);
    },
    onQuery: (typed) => {
      const taken = takeChip(chip, typed);
      if (taken.chip !== chip) {
        setChip(taken.chip);
        handle.api.replaceQuery(taken.text);
        setText(taken.text);
        return;
      }
      setText(typed);
    },
    onEmptyBackspace: () => {
      if (chip != null) setChip(null);
    },
    onOpenChangeComplete: (opened) => {
      if (!opened) onClosed();
    },
  });

  const firstKey = keyed[0]?.value ?? null;
  const shownKeys = keyed.map(({ value }) => value).join("\n");
  useEffect(() => {
    handle.api.listbox.move("first");
  }, [shownKeys, firstKey, handle.api.listbox]);

  let at = 0;
  return usePart(
    handle,
    "root",
    {},
    {
      children: (
        <>
        <Backdrop className="fixed inset-0 z-50 bg-scrim" />
        <Content
          onKeyDown={(event) => {
            if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey)) return;
            event.preventDefault();
            close();
          }}
          initialFocus={`#${CSS.escape(inputId)}`}
          finalFocus={() => returnTo()}
          className="fixed inset-x-0 top-[120px] z-50 mx-auto flex max-h-[calc(100vh-160px)] w-[min(640px,calc(100vw-32px))] flex-col overflow-hidden rounded-[12px] border border-rule bg-raised text-ink shadow-[0_24px_60px_rgba(0,0,0,.35)]"
        >
          <Title className="sr-only">Command palette</Title>
          <div className="flex h-14 shrink-0 items-center gap-3 border-b border-rule-2 px-4">
            <svg viewBox="0 0 16 16" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true" className="shrink-0 text-ink-3">
              <circle cx="7" cy="7" r="4.5" />
              <path d="m10.5 10.5 3 3" />
            </svg>
            {chip && (
              <span data-chip className="shrink-0 rounded-[6px] border border-chip-border bg-selected px-2 py-0.5 text-[12px] font-medium text-ink">
                {CHIPS[chip]}
              </span>
            )}
            <label htmlFor={inputId} className="sr-only">
              Search or jump to
            </label>
            <Input
              id={inputId}
              placeholder={chip ? `Search ${CHIPS[chip].toLowerCase()}` : "Search or jump to…"}
              autoComplete="off"
              spellCheck={false}
              className="min-w-0 flex-1 bg-transparent text-[17px] text-ink outline-none! placeholder:text-ink-3"
            />
            <kbd className={KBD}>esc</kbd>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {confirming ? (
              <Confirm entry={confirming} busy={sweep.isPending} refusal={refusal} onConfirm={() => void confirm()} onCancel={cancel} />
            ) : (
              <>
                {listing.loading && (
                  <>
                    <p role="status" className="sr-only">Searching…</p>
                    <Skeleton />
                  </>
                )}
                {listing.empty && <p role="status" className="px-4 pt-4 pb-2 text-[13px] text-ink-3">{listing.empty}</p>}
                <List aria-label="Results" className="flex flex-col px-1.5 pb-1.5">
                  {listing.groups.map((group, index) => {
                    const headingId = `${inputId}-group-${index}`;
                    return (
                      <div key={group.name} role="group" aria-labelledby={headingId} className="pt-1.5">
                        <div id={headingId} role="presentation" className="px-2.5 pt-2 pb-1 text-[12px] font-semibold text-ink-2">
                          {group.name}
                        </div>
                        {group.entries.map((entry) => {
                          const value = keyed[at++]!.value;
                          return (
                            <Option
                              key={value}
                              value={value}
                              title={entry.disabled}
                              className="group flex min-h-12 cursor-pointer items-center gap-3 rounded-[8px] px-2.5 py-1.5 aria-disabled:cursor-default aria-disabled:opacity-55 data-[active=true]:bg-selected"
                            >
                              <span className="flex size-4 shrink-0 items-center justify-center">
                                <Glyph glyph={entry.glyph} />
                              </span>
                              <span className="flex min-w-0 flex-1 flex-col">
                                <span data-title className="truncate text-[14px] leading-[1.35] text-ink">
                                  <Marked text={entry.title} query={query} />
                                </span>
                                <span className="truncate font-mono text-[12px] leading-[1.4] text-ink-3">{entry.meta}</span>
                              </span>
                              <kbd aria-hidden="true" className={`${KBD} invisible group-data-[active=true]:visible`}>↵</kbd>
                            </Option>
                          );
                        })}
                      </div>
                    );
                  })}
                </List>
              </>
            )}
          </div>
          <div className="flex h-11 shrink-0 items-center gap-2 border-t border-rule-2 px-3.5">
            <span className="flex gap-1">
              <Hint keys="↑" tip="Move up" />
              <Hint keys="↓" tip="Move down" />
            </span>
            <Hint keys="↵" tip="Open the highlighted item" />
            <Hint keys=">" tip="Type > first for actions" />
            <Hint keys="#" tip="Type # first for tickets" />
            <Hint keys="@" tip="Type @ first for runs" />
            <span className="ml-auto">
              <Hint keys="⌘K" tip="Open or close the palette (Ctrl+K off macOS)" />
            </span>
          </div>
        </Content>
        </>
      ),
    },
    "div",
  );
}
