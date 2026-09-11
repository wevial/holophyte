import { useEffect, useId, useRef, useState } from "react";
import { ACTIONS_OFF, ROUTES, postAction } from "../lib/actions";
import { fetchConfig, namedKey, putConfig, type ConfigAnswer, type ConfigValues, type ConfigWrite, type PatchValue } from "../lib/config";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { Status } from "../lib/types";
import { ActionButton } from "./ActionButton";
import { NEEDS_TOKEN } from "./TicketSheet";

/** The line a read-only sheet shows: the key that would open it. */
export const CONFIG_EDIT_OFF = "Read-only: this daemon has not opted into config edits ([serve] config_edit = true)";
/** The banner over Save: the loop reads the file once, at startup. */
export const APPLIES_LINE = "A saved change applies at the loop's next start.";
/** The action beside the banner, the same wired label the rows post. */
export const RESTART_LABEL = "Restart supervisor";
/** The note under a field whose key the file holds in another shape than
 *  the field edits (a table where a string is expected, a list of numbers):
 *  the field shows the value as the daemon parsed it, read-only. */
export const UNBOUND_NOTE = "edit this one in the raw tab";
/** The line over the fields when the daemon could not parse the file. */
export const UNPARSED_LINE = "The file does not parse as TOML; the fields are empty until the raw tab fixes it.";

type FieldKind = { kind: "text" } | { kind: "number" } | { kind: "lines" } | { kind: "select"; options: readonly string[] };

/** One typed field: the `[table] key` it binds and how it is drawn. */
export type Field = { table: string; key: string; label: string } & FieldKind;

/** The keys the sheet binds, in the order they are drawn; every other
 *  key stays editable in the raw tab. The option lists are the loader's
 *  (`holophyte/config.py`: `REVIEW_EFFORTS`, `MERGE_METHODS`, the
 *  `[merge] mode` and `approve` pairs of `docs/config.md`). */
export const FIELDS: readonly Field[] = [
  { table: "agents", key: "implementer", label: "Implementer command", kind: "text" },
  { table: "agents", key: "review_model", label: "Review model", kind: "text" },
  { table: "agents", key: "review_effort", label: "Review effort", kind: "select", options: ["low", "medium", "high", "xhigh"] },
  { table: "worktree", key: "setup", label: "Worktree setup", kind: "lines" },
  { table: "loop", key: "workers", label: "Workers", kind: "number" },
  { table: "merge", key: "mode", label: "Merge mode", kind: "select", options: ["local", "pr"] },
  { table: "merge", key: "approve", label: "Approval", kind: "select", options: ["auto", "human"] },
  { table: "merge", key: "pr_merge_method", label: "PR merge method", kind: "select", options: ["merge", "squash", "rebase"] },
  { table: "merge", key: "after", label: "After merge", kind: "lines" },
];

const fieldId = (field: Field) => `${field.table}.${field.key}`;

/** What the daemon's `values` hold for a field: `absent` when the key is
 *  not in the file, the typed value when it is in the field's shape, or
 *  `unbound` with the value as parsed when it is in another shape. */
type Bound = { state: "absent" } | { state: "bound"; value: PatchValue } | { state: "unbound"; value: unknown };

const isLines = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");

function bind(field: Field, values: ConfigValues | null): Bound {
  const table = values?.[field.table];
  if (table == null || typeof table !== "object" || !(field.key in table)) return { state: "absent" };
  const value: unknown = table[field.key];
  const fits =
    field.kind === "number" ? typeof value === "number" && Number.isInteger(value) : field.kind === "lines" ? isLines(value) : typeof value === "string";
  return fits ? { state: "bound", value: value as PatchValue } : { state: "unbound", value };
}

const same = (a: PatchValue, b: PatchValue) => (Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((item, at) => item === b[at]) : a === b);

type Verdict = { ok: true; detail: string } | { ok: false; error: string; at: string | null };

/**
 * A project's settings, opened from its Floor block: the ticket sheet's
 * frame over the daemon's `GET /config`. The Fields tab binds `FIELDS`
 * to the daemon's parsed `values`; an edit is held as that key's new
 * value and Save sends the changed keys alone as a `PUT /config`
 * `patch` of dotted keys, so the console never parses or rewrites TOML.
 * The Raw tab is the whole `text`, and a save from it sends `text`
 * instead: the two drafts are exclusive, an edit in one tab discarding
 * the other's. An accepted save re-reads `GET /config`, so the fields
 * show the file as the daemon wrote it. The daemon's verdict shows
 * inline: a refused patch under the field it names (the Fields tab
 * selected) or under the raw tab; a refused raw draft always under the
 * raw tab, the draft kept for correction. A daemon whose `/status` lacks `config_edit`
 * draws every field read-only under a line naming the key. Escape, the
 * backdrop or the close button calls `onClose`; focus moves to the panel
 * on open.
 */
export function SettingsSheet({
  base,
  name,
  path,
  status,
  onClose,
  deps = defaultPollDeps,
}: {
  base: string;
  name: string;
  path: string;
  status: Pick<Status, "actions" | "config_edit">;
  onClose: () => void;
  deps?: { fetch: Fetch };
}) {
  const editable = status.config_edit === true;
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const [answer, setAnswer] = useState<ConfigAnswer | null>(null);
  const [text, setText] = useState("");
  const [tab, setTab] = useState<"fields" | "raw">("fields");
  const [edits, setEdits] = useState<Record<string, PatchValue>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [restart, setRestart] = useState<{ text: string; ok: boolean } | null>(null);
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;

  useEffect(() => {
    panel.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const load = async () => {
    const result = await fetchConfig(base, fetchRef.current);
    setAnswer(result);
    if (result.state === "ok") setText(result.config.text);
    setEdits({});
    setDrafts({});
  };

  useEffect(() => {
    let alive = true;
    void fetchConfig(base, fetchRef.current).then((result) => {
      if (!alive) return;
      setAnswer(result);
      if (result.state === "ok") setText(result.config.text);
    });
    return () => {
      alive = false;
    };
  }, [base]);

  const loaded = answer?.state === "ok";
  const config = answer?.state === "ok" ? answer.config : null;
  const values = config?.values ?? null;
  const rawDirty = config != null && text !== config.text;
  const dirty = rawDirty || Object.keys(edits).length > 0;

  /** Hold `value` as the field's edit, or drop the edit when it is the
   *  file's own value again, so the patch carries changed keys alone. */
  const edit = (field: Field, value: PatchValue | null) => {
    if (config != null && rawDirty) setText(config.text);
    const id = fieldId(field);
    const bound = bind(field, values);
    const unchanged = value == null ? bound.state === "absent" : bound.state === "bound" && same(bound.value, value);
    setEdits(({ [id]: _dropped, ...rest }) => (unchanged || value == null ? rest : { ...rest, [id]: value }));
  };

  const save = async () => {
    setVerdict(null);
    const write: ConfigWrite = rawDirty ? { text } : { patch: edits };
    const result = await putConfig(base, write, fetchRef.current);
    if (result.ok) {
      await load();
      setVerdict({ ok: true, detail: `Saved; applies at the ${result.applies}${result.backup ? `; previous text in ${result.backup}` : ""}` });
      return;
    }
    // A refused raw draft is corrected in the raw tab, whatever key the
    // daemon names: the fields show the file, not the draft, and a field
    // edit would discard the draft's other changes. A refused patch lands
    // under the field it names, or under the raw tab when it names none,
    // so the sentence is never rendered unseen.
    const at = result.refused && !rawDirty ? namedKey(result.error) : null;
    const field = at != null && FIELDS.some((candidate) => fieldId(candidate) === at) ? at : null;
    setVerdict({ ok: false, error: result.error, at: field });
    setTab(field == null ? "raw" : "fields");
  };

  const restartRoute = ROUTES[RESTART_LABEL]!;
  const restartAct =
    status.actions === true
      ? async () => {
          setRestart(null);
          const result = await postAction(base, restartRoute, {}, deps.fetch);
          setRestart({ text: result.detail, ok: result.ok });
        }
      : undefined;

  const errorFor = (id: string) => (verdict != null && !verdict.ok && verdict.at === id ? verdict.error : null);
  const rawError = verdict != null && !verdict.ok && verdict.at == null ? verdict.error : null;
  const inert = !editable || !loaded || values == null;

  const control = (field: Field) => {
    const id = fieldId(field);
    const error = errorFor(id);
    const bound = bind(field, values);
    const common = {
      id,
      "data-field": id,
      "aria-invalid": error != null || undefined,
      "aria-describedby": error != null ? `${id}-error` : undefined,
      className: "w-full rounded-button border border-chip-border bg-card px-2 py-1 font-mono text-[12px] text-ink disabled:opacity-60 read-only:opacity-60",
    };
    if (bound.state === "unbound") {
      // Present, but in another shape than the field edits: shown as the
      // daemon parsed it, read-only, so a typed edit never overwrites it.
      return (
        <>
          <input {...common} type="text" readOnly data-unbound aria-describedby={`${id}-unbound`} value={JSON.stringify(bound.value)} />
          <p id={`${id}-unbound`} data-unbound-note={id} className="font-mono text-[11px] text-muted">
            {UNBOUND_NOTE}
          </p>
        </>
      );
    }
    const value: PatchValue | null = id in edits ? edits[id]! : bound.state === "bound" ? bound.value : null;
    if (field.kind === "select") {
      return (
        <select {...common} disabled={inert} value={typeof value === "string" ? value : ""} onChange={(event) => edit(field, event.target.value === "" ? null : event.target.value)}>
          <option value="">(not set)</option>
          {field.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    }
    if (field.kind === "lines") {
      const lines = isLines(value) ? value : [];
      return (
        <textarea
          {...common}
          rows={Math.max(2, lines.length + 1)}
          readOnly={inert}
          value={drafts[id] ?? lines.join("\n")}
          onChange={(event) => {
            setDrafts((current) => ({ ...current, [id]: event.target.value }));
            edit(
              field,
              event.target.value.split("\n").filter((line) => line.trim() !== ""),
            );
          }}
          onBlur={() => setDrafts(({ [id]: _dropped, ...rest }) => rest)}
        />
      );
    }
    if (field.kind === "number") {
      return (
        <input
          {...common}
          type="number"
          min={1}
          step={1}
          readOnly={inert}
          value={typeof value === "number" ? value : ""}
          onChange={(event) => {
            const parsed = Number.parseInt(event.target.value, 10);
            edit(field, event.target.value === "" || Number.isNaN(parsed) ? null : parsed);
          }}
        />
      );
    }
    return <input {...common} type="text" readOnly={inert} value={typeof value === "string" ? value : ""} onChange={(event) => edit(field, event.target.value === "" ? null : event.target.value)} />;
  };

  return (
    <div data-settings-sheet className="fixed inset-0 z-40">
      <div data-backdrop aria-hidden="true" onClick={onClose} className="absolute inset-0 bg-ink/40" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="absolute inset-y-0 right-0 flex w-[480px] max-w-full flex-col overflow-hidden border-l border-line bg-card shadow-card outline-none"
      >
        <header className="flex flex-col gap-[6px] border-b border-line px-5 py-4">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[12px] font-semibold text-ink">Settings</span>
            <span className="truncate font-mono text-[11px] text-faint">{config?.path || path}</span>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="ml-auto rounded-button border border-chip-border px-2 py-[2px] text-[12px] font-semibold text-ink"
            >
              ×
            </button>
          </div>
          <h2 id={titleId} className="text-[15px] font-semibold leading-[1.35] text-ink">
            {name}
          </h2>
          <div role="tablist" className="flex gap-1">
            {(["fields", "raw"] as const).map((choice) => (
              <button
                key={choice}
                type="button"
                role="tab"
                aria-selected={tab === choice}
                onClick={() => setTab(choice)}
                className={`rounded-button border px-2 py-[2px] text-[12px] font-semibold ${
                  tab === choice ? "border-ink bg-well text-ink" : "border-chip-border text-muted"
                }`}
              >
                {choice === "fields" ? "Fields" : "Raw TOML"}
              </button>
            ))}
          </div>
        </header>
        <div data-sheet-body className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-[13px] leading-[1.5] text-body">
          {!editable && (
            <p data-config-edit-off className="mb-3 font-mono text-[11px] text-muted">
              {CONFIG_EDIT_OFF}
            </p>
          )}
          {answer == null ? (
            <p className="text-muted">Loading the configuration…</p>
          ) : answer.state === "needs_token" ? (
            <p data-needs-token-line className="font-mono text-[12px] text-muted">
              {NEEDS_TOKEN}
            </p>
          ) : answer.state === "off" ? (
            <p data-config-off className="text-muted">
              This daemon does not serve its configuration.
            </p>
          ) : answer.state === "error" ? (
            <p role="alert" className="font-mono text-[11px] text-bad-text">
              config failed: {answer.error}
            </p>
          ) : answer.config.values == null ? (
            <p data-unparsed className="mb-3 font-mono text-[11px] text-muted">
              {UNPARSED_LINE}
            </p>
          ) : null}
          {tab === "fields" ? (
            <div role="tabpanel" data-tab="fields" className="flex flex-col gap-3">
              {FIELDS.map((field) => {
                const id = fieldId(field);
                const error = errorFor(id);
                return (
                  <div key={id} className="flex flex-col gap-1">
                    <label htmlFor={id} className="text-[12px] font-semibold text-ink">
                      {field.label}
                      <span className="ml-2 font-mono text-[11px] font-normal text-faint">
                        [{field.table}] {field.key}
                      </span>
                    </label>
                    {control(field)}
                    {error != null && (
                      <p id={`${id}-error`} role="alert" data-field-error={id} className="font-mono text-[11px] text-bad-text">
                        {error}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div role="tabpanel" data-tab="raw" className="flex flex-col gap-1">
              <textarea
                aria-label="Raw TOML"
                data-raw
                readOnly={!editable || !loaded}
                value={text}
                rows={Math.max(12, text.split("\n").length + 1)}
                onChange={(event) => {
                  setEdits({});
                  setDrafts({});
                  setText(event.target.value);
                }}
                className="w-full rounded-button border border-chip-border bg-card px-2 py-1 font-mono text-[12px] text-ink read-only:opacity-60"
              />
              {rawError != null && (
                <p role="alert" data-raw-error className="font-mono text-[11px] text-bad-text">
                  {rawError}
                </p>
              )}
            </div>
          )}
        </div>
        <footer className="flex flex-col gap-2 border-t border-line px-5 py-3">
          <div className="flex items-center gap-2">
            <p data-applies className="text-[12px] text-muted">
              {APPLIES_LINE}
            </p>
            <span className="ml-auto">
              <ActionButton onAct={restartAct} title={ACTIONS_OFF}>
                {RESTART_LABEL}
              </ActionButton>
            </span>
          </div>
          {restart && (
            <p data-restart-detail data-ok={restart.ok} role="status" className={`text-right text-[12px] ${restart.ok ? "text-muted" : "text-needs-you-link"}`}>
              {restart.text}
            </p>
          )}
          <div className="flex items-center gap-2">
            {verdict?.ok && (
              <p data-save-verdict role="status" className="text-[12px] text-ok-text">
                {verdict.detail}
              </p>
            )}
            <span className="ml-auto">
              <ActionButton onAct={editable && loaded ? save : undefined} disabled={!editable || !loaded || !dirty} title={editable ? undefined : CONFIG_EDIT_OFF}>
                Save
              </ActionButton>
            </span>
          </div>
        </footer>
      </div>
    </div>
  );
}
