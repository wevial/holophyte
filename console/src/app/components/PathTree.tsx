import { useMemo } from "react";
import {
  Tree,
  TreeGroup,
  TreeItem,
  TreeItemCheckbox,
  TreeItemIndicator,
  TreeItemLabel,
  type TreeItemData,
} from "@ui-sdk/react/tree";
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

type Model = { under: Map<string, string[]>; patterns: Map<string, string>; items: TreeItemData[]; total: number };
type Split = { selected: string[]; kept: string[]; keptCovered: Set<string> };

const join = (dir: string, name: string) => (dir === "" ? name : `${dir}/${name}`);

function modelOf(tree: TrackedTree): Model {
  const dirs = new Map(tree.dirs.map((dir) => [dir.path, dir]));
  const under = new Map<string, string[]>();
  const build = (path: string): TreeItemData[] => {
    const dir = dirs.get(path);
    if (!dir) return [];
    const files: string[] = [];
    const children: TreeItemData[] = [];
    for (const name of dir.dirs) {
      const child = join(path, name);
      children.push({ value: child, label: name, children: build(child) });
      files.push(...under.get(child)!);
    }
    for (const leaf of dir.leaves) {
      const file = join(path, leaf);
      children.push({ value: file, label: leaf });
      files.push(file);
    }
    under.set(path, files);
    return children;
  };
  const items = build("");
  const patterns = new Map<string, string>();
  for (const [path, files] of under) {
    if (path !== "") patterns.set(`${literal(path)}/*`, path);
    for (const file of files) patterns.set(literal(file), file);
  }
  return { under, patterns, items, total: under.get("")?.length ?? 0 };
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
  return { selected: [...selected], kept, keptCovered };
}

function written(items: readonly TreeItemData[], selected: ReadonlySet<string>): string[] {
  return items.flatMap((item) => {
    if (selected.has(item.value)) return [item.children ? `${literal(item.value)}/*` : literal(item.value)];
    return item.children ? written(item.children, selected) : [];
  });
}

/** Rewrite the tree's patterns from `checked` while keeping patterns the
 *  tree cannot show in their place. */
function merged(items: readonly TreeItemData[], value: readonly string[], kept: ReadonlySet<string>, checked: readonly string[]): string[] {
  const next = written(items, new Set(checked));
  const stays = new Set([...kept, ...next]);
  return [...new Set([...value.filter((pattern) => stays.has(pattern)), ...next])];
}

function coveredBy(model: Model, selected: readonly string[]): Set<string> {
  return new Set(selected.flatMap((path) => model.under.get(path) ?? [path]));
}

const counted = (count: number) => count.toLocaleString("en-US");
const plural = (count: number) => `${counted(count)} ${count === 1 ? "file" : "files"}`;

function FileGlyph() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-4 shrink-0">
      <path d="M4 1.5h5.5L12.5 4.5v10h-8.5ZM9.5 1.5v3h3" />
    </svg>
  );
}

const ITEM = "outline-none! [&:focus-visible>[data-slot=tree-label]]:outline-2 [&:focus-visible>[data-slot=tree-label]]:-outline-offset-2 [&:focus-visible>[data-slot=tree-label]]:outline-focus";
const ROW = "flex min-h-7 cursor-pointer items-center gap-1.5 pr-2 pl-[calc((var(--tree-level,1)-1)*16px)] select-none hover:bg-paper-2";
const CHECKBOX = "inline-flex size-3.5 shrink-0 cursor-pointer items-center justify-center border data-unchecked:border-ink-3 data-unchecked:bg-paper data-checked:border-act data-checked:bg-act data-checked:text-paper data-indeterminate:border-act data-indeterminate:bg-act data-indeterminate:text-paper";

function PathBranch({ items, under }: { items: readonly TreeItemData[]; under: ReadonlyMap<string, string[]> }) {
  return items.map((item) => {
    const branch = item.children !== undefined;
    return (
      <TreeItem
        key={item.value}
        value={item.value}
        aria-label={branch ? `${item.value}/` : item.value}
        // Drop UI SDK's label reference so `aria-label` names the item by its full path.
        render={({ "aria-labelledby": _, ...props }) => <div {...props} />}
        className={ITEM}
      >
        <TreeItemLabel className={ROW}>
          {branch ? (
            <TreeItemIndicator className="group inline-flex cursor-pointer">
              {(state) => <FoldChevron open={state.expanded} />}
            </TreeItemIndicator>
          ) : (
            <span aria-hidden="true" className="size-3.5 shrink-0" />
          )}
          <TreeItemCheckbox className={CHECKBOX}>
            {(state) => state.checked !== false && (
              <svg viewBox="0 0 16 16" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d={state.checked === true ? "M3.5 8.5l3 3 6-7" : "M4 8h8"} />
              </svg>
            )}
          </TreeItemCheckbox>
          {branch ? <FolderIcon className="text-ink-3" /> : <FileGlyph />}
          <span className={`min-w-0 flex-1 truncate ${branch ? "text-ink" : "text-ink-2"}`}>{branch ? `${item.label}/` : item.label}</span>
          <span aria-hidden="true" className="tabular-nums text-ink-3">{counted(under.get(item.value)?.length ?? 1)}</span>
        </TreeItemLabel>
        {item.children && <TreeGroup><PathBranch items={item.children} under={under} /></TreeGroup>}
      </TreeItem>
    );
  });
}

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
  const kept = new Set(state.kept);
  const covered = new Set([...coveredBy(model, state.selected), ...state.keptCovered]).size;
  return (
    <div className="flex w-full min-w-0 flex-col gap-2 font-mono text-[12px]">
      <Tree
        aria-label={`${label} tree`}
        items={model.items}
        mode="none"
        checkable
        checkStrategy="trunk"
        checked={state.selected}
        onCheckedChange={(next) => onChange(merged(model.items, value, kept, next))}
        className="max-h-96 w-full overflow-auto border border-rule bg-paper py-1 pl-1"
      >
        <PathBranch items={model.items} under={model.under} />
      </Tree>
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
