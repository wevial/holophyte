import { useMemo, useState, type ReactNode } from "react";
import type { TrackedTree } from "../../lib/contract.gen";
import { FoldChevron, FolderIcon } from "./icons";

type PathView = "text" | "tree";

const VIEW_KEY = "holophyte.next.settings.ui-paths.view";

export function readPathView(): PathView {
  try {
    return localStorage.getItem(VIEW_KEY) === "tree" ? "tree" : "text";
  } catch {
    return "text";
  }
}

export function writePathView(view: PathView): void {
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    // Storage may be disabled; the choice still holds for this page.
  }
}

type Model = { dirs: Map<string, TrackedTree["dirs"][number]>; under: Map<string, string[]>; patterns: Map<string, string>; total: number };
type Split = { selected: Set<string>; kept: string[]; keptCovered: Set<string> };
type Checked = "true" | "false" | "mixed";

const join = (dir: string, name: string) => (dir === "" ? name : `${dir}/${name}`);

function modelOf(tree: TrackedTree): Model {
  const dirs = new Map(tree.dirs.map((dir) => [dir.path, dir]));
  const under = new Map<string, string[]>();
  const collect = (path: string): string[] => {
    const dir = dirs.get(path);
    if (!dir) return [];
    const files = [...dir.dirs.flatMap((name) => collect(join(path, name))), ...dir.leaves.map((leaf) => join(path, leaf))];
    under.set(path, files);
    return files;
  };
  collect("");
  const patterns = new Map<string, string>();
  for (const [path, files] of under) {
    if (path !== "") patterns.set(`${literal(path)}/*`, path);
    for (const file of files) patterns.set(literal(file), file);
  }
  return { dirs, under, patterns, total: under.get("")?.length ?? 0 };
}

const inSet = (text: string) => text.replace(/[\\\]-]/g, "\\$&");

/** A bracket set's body as Python's `fnmatch.translate` reads it: a
 *  reversed range is dropped rather than refused. */
function bracket(pattern: string, start: number, end: number): string {
  const body = pattern.slice(start, end);
  let set = inSet(body);
  if (body.includes("-")) {
    const chunks: string[] = [];
    let from = start;
    const step = (index: number) => index + ((pattern.codePointAt(index) ?? 0) > 0xffff ? 2 : 1);
    let at = pattern[start] === "!" ? step(start + 1) : step(start);
    for (;;) {
      at = pattern.indexOf("-", at);
      if (at < 0 || at >= end) break;
      chunks.push(pattern.slice(from, at));
      from = at + 1;
      at = step(step(from));
    }
    const rest = pattern.slice(from, end);
    if (rest) chunks.push(rest);
    else chunks[chunks.length - 1] += "-";
    for (let k = chunks.length - 1; k > 0; k--) {
      const [before, after] = [Array.from(chunks[k - 1]!), Array.from(chunks[k]!)];
      if (before.at(-1)!.codePointAt(0)! > after[0]!.codePointAt(0)!) {
        chunks[k - 1] = before.slice(0, -1).join("") + after.slice(1).join("");
        chunks.splice(k, 1);
      }
    }
    set = chunks.map(inSet).join("-");
  }
  if (set === "") return "(?!)";
  if (set === "!") return ".";
  return `[${set.startsWith("!") ? `^${set.slice(1)}` : set.startsWith("^") || set.startsWith("[") ? `\\${set}` : set}]`;
}

type Glob = "*" | ((char: string) => boolean);

/** Python's `fnmatch.fnmatchcase`, the matcher `[merge] ui_paths` is read
 *  with, over code points. A mismatch resumes only at the latest `*`, so
 *  a match costs at most the path's length times the pattern's. */
function fnmatch(pattern: string): { test: (path: string) => boolean } {
  const globs: Glob[] = [];
  for (let at = 0; at < pattern.length; at++) {
    const char = String.fromCodePoint(pattern.codePointAt(at)!);
    at += char.length - 1;
    if (char === "*") globs.push("*");
    else if (char === "?") globs.push(() => true);
    else if (char === "[") {
      let end = at + 1;
      if (pattern[end] === "!") end++;
      if (pattern[end] === "]") end++;
      end = pattern.indexOf("]", end);
      if (end < 0) {
        globs.push((each) => each === "[");
        continue;
      }
      const set = new RegExp(`^${bracket(pattern, at + 1, end)}$`, "su");
      globs.push((each) => set.test(each));
      at = end;
    } else globs.push((each) => each === char);
  }
  return {
    test: (path) => {
      const chars = Array.from(path);
      let [glob, at, star, resume] = [0, 0, -1, 0];
      while (at < chars.length) {
        const here = globs[glob];
        if (here === "*") [star, resume, glob] = [glob, at, glob + 1];
        else if (here !== undefined && here(chars[at]!)) [glob, at] = [glob + 1, at + 1];
        else if (star >= 0) [glob, at, resume] = [star + 1, resume + 1, resume + 1];
        else return false;
      }
      while (globs[glob] === "*") glob++;
      return glob === globs.length;
    },
  };
}

/** `path` as a pattern matching only itself under `fnmatchcase`. */
const literal = (path: string) => path.replace(/[[*?]/g, "[$&]");

const nodeOf = (model: Model, pattern: string) => model.patterns.get(pattern) ?? null;

const ancestors = (path: string) => path.split("/").slice(0, -1).map((_, at, parts) => parts.slice(0, at + 1).join("/"));
const picked = (selected: Set<string>, path: string) => selected.has(path) || ancestors(path).some((each) => selected.has(each));

function split(model: Model, value: readonly string[]): Split {
  const files = model.under.get("") ?? [];
  const selected = new Set<string>();
  const kept: string[] = [];
  for (const pattern of value) {
    const node = nodeOf(model, pattern);
    if (node !== null) selected.add(node);
    else kept.push(pattern);
  }
  for (const each of [...selected]) if (ancestors(each).some((above) => selected.has(above))) selected.delete(each);
  const matchers = kept.map(fnmatch);
  const keptCovered = new Set(files.filter((file) => matchers.some((matcher) => matcher.test(file))));
  return { selected, kept, keptCovered };
}

function written(model: Model, selected: Set<string>, path = ""): string[] {
  const dir = model.dirs.get(path)!;
  const folders = dir.dirs.flatMap((name) => {
    const child = join(path, name);
    return selected.has(child) ? [`${literal(child)}/*`] : written(model, selected, child);
  });
  return [...folders, ...dir.leaves.map((leaf) => join(path, leaf)).filter((file) => selected.has(file)).map(literal)];
}

function uncheck(model: Model, selected: Set<string>, path: string) {
  if (selected.delete(path)) return;
  const from = ancestors(path).find((each) => selected.has(each))!;
  selected.delete(from);
  for (const step of [...ancestors(path), path].filter((each) => each.length > from.length)) {
    const parent = model.dirs.get(step.slice(0, step.lastIndexOf("/")))!;
    for (const sibling of [...parent.dirs, ...parent.leaves].map((name) => join(parent.path, name))) {
      if (sibling !== step) selected.add(sibling);
    }
  }
}

/** `value` with `path` checked, or unchecked when it or a folder above it
 *  is: the tree's patterns rewritten as the shortest list, each pattern
 *  the tree cannot show kept in its place. */
function toggled(model: Model, value: readonly string[], path: string): string[] {
  const { selected, kept } = split(model, value);
  if (picked(selected, path)) uncheck(model, selected, path);
  else {
    for (const each of [...selected]) if (each.startsWith(`${path}/`)) selected.delete(each);
    selected.add(path);
  }
  const next = written(model, selected);
  const stays = new Set([...kept, ...next]);
  return [...new Set([...value.filter((pattern) => stays.has(pattern)), ...next])];
}

function coveredBy(model: Model, selected: Set<string>): Set<string> {
  return new Set([...selected].flatMap((path) => model.under.get(path) ?? [path]));
}

function checkedOf(path: string, files: readonly string[], { selected, keptCovered }: Split): Checked {
  if (picked(selected, path)) return "true";
  const inside = [...selected].some((each) => each.startsWith(`${path}/`));
  return inside || files.some((file) => keptCovered.has(file)) ? "mixed" : "false";
}

const counted = (count: number) => count.toLocaleString("en-US");
const plural = (count: number) => `${counted(count)} ${count === 1 ? "file" : "files"}`;

function Box({ checked, name, onToggle }: { checked: Checked; name: string; onToggle: () => void }) {
  return (
    <button type="button" role="checkbox" aria-checked={checked} aria-label={name} onClick={onToggle}
      className={`inline-flex size-3.5 shrink-0 cursor-pointer items-center justify-center border ${checked === "false" ? "border-ink-3 bg-paper" : "border-act bg-act text-paper"}`}>
      {checked !== "false" && <svg viewBox="0 0 16 16" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d={checked === "true" ? "M3.5 8.5l3 3 6-7" : "M4 8h8"} />
      </svg>}
    </button>
  );
}

function FileGlyph() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-4 shrink-0">
      <path d="M4 1.5h5.5L12.5 4.5v10h-8.5ZM9.5 1.5v3h3" />
    </svg>
  );
}

const ROW = "flex min-h-7 items-center gap-1.5 pr-2 hover:bg-paper-2";
const INDENT_PX = 16;

export function PathViewSwitch({ label, view, onPick }: { label: string; view: PathView; onPick: (view: PathView) => void }) {
  return (
    <div role="group" aria-label={`${label} view`} className="inline-flex border border-rule bg-paper [&>*+*]:border-l [&>*+*]:border-rule">
      {(["text", "tree"] as const).map((each) => <button key={each} type="button" aria-pressed={view === each} onClick={() => onPick(each)}
        className={`min-h-8 cursor-pointer px-3 font-mono text-[12px] ${view === each ? "bg-act-wash text-act" : "text-ink-2 hover:text-ink"}`}>
        {each === "text" ? "Text" : "Tree"}
      </button>)}
    </div>
  );
}

export function PathTree({ tree, value, label, onChange }: { tree: TrackedTree; value: readonly string[]; label: string; onChange: (value: string[]) => void }) {
  const model = useMemo(() => modelOf(tree), [tree]);
  const state = useMemo(() => split(model, value), [model, value]);
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());
  const flip = (path: string) => setOpen((now) => {
    const next = new Set(now);
    if (!next.delete(path)) next.add(path);
    return next;
  });
  const rows = (path: string, depth: number): ReactNode[] => {
    const dir = model.dirs.get(path)!;
    const indent = { paddingLeft: depth * INDENT_PX };
    return [
      ...dir.dirs.map((name) => {
        const child = join(path, name);
        const files = model.under.get(child)!;
        const opened = open.has(child);
        return <li key={child}>
          <div className={ROW} style={indent}>
            <button type="button" aria-label={`${opened ? "Close" : "Open"} ${child}/`} aria-expanded={opened} onClick={() => flip(child)} className="group inline-flex cursor-pointer"><FoldChevron open={opened} /></button>
            <Box checked={checkedOf(child, files, state)} name={`${child}/`} onToggle={() => onChange(toggled(model, value, child))} />
            <FolderIcon className="text-ink-3" />
            <span className="min-w-0 flex-1 truncate text-ink">{name}/</span>
            <span className="tabular-nums text-ink-3">{counted(files.length)}</span>
          </div>
          {opened && <ul>{rows(child, depth + 1)}</ul>}
        </li>;
      }),
      ...dir.leaves.map((leaf) => {
        const file = join(path, leaf);
        return <li key={file}>
          <div className={ROW} style={indent}>
            <span aria-hidden="true" className="size-3.5 shrink-0" />
            <Box checked={checkedOf(file, [file], state)} name={file} onToggle={() => onChange(toggled(model, value, file))} />
            <FileGlyph />
            <span className="min-w-0 flex-1 truncate text-ink-2">{leaf}</span>
            <span className="tabular-nums text-ink-3">1</span>
          </div>
        </li>;
      }),
    ];
  };
  const kept = new Set(state.kept);
  const covered = new Set([...coveredBy(model, state.selected), ...state.keptCovered]).size;
  return (
    <div className="flex w-full min-w-0 flex-col gap-2 font-mono text-[12px]">
      <ul aria-label={`${label} tree`} className="max-h-96 w-full overflow-auto border border-rule bg-paper py-1 pl-1">{rows("", 0)}</ul>
      {value.length > 0 && <ul aria-label={`${label} patterns`} className="flex w-full min-w-0 flex-col gap-1">
        {value.map((pattern, index) => {
          const node = nodeOf(model, pattern);
          return <li key={index} className="break-words text-ink-2">
            {pattern} · <span className="text-ink-3">{kept.has(pattern) ? "kept from text" : plural(node === null ? 0 : model.under.get(node)?.length ?? 1)}</span>
          </li>;
        })}
      </ul>}
      <p aria-label={`${label} covered`} className="text-ink-3">{counted(covered)} of {counted(model.total)} tracked {model.total === 1 ? "file" : "files"}</p>
    </div>
  );
}
