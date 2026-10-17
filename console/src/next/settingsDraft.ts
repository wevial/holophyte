import { createContext, createElement, useContext, useState, type ReactNode } from "react";
import { same, type PatchValue } from "../lib/config";

export type SettingsEdit = { old: PatchValue; value: PatchValue };
type ProjectEdits = Record<string, SettingsEdit>;
type DraftContext = {
  raw: Record<string, string>;
  editRaw: (project: string, text: string | null) => void;
  projects: Record<string, ProjectEdits>;
  edit: (project: string, key: string, old: PatchValue, value: PatchValue) => void;
  discard: (project: string) => void;
  saved: (project: string, submitted: ProjectEdits) => void;
};
const Context = createContext<DraftContext | null>(null);

export function SettingsDraftProvider({ children }: { children: ReactNode }) {
  const [raw, setRaw] = useState<Record<string, string>>({});
  const editRaw = (project: string, text: string | null) => setRaw((previous) => {
    const next = { ...previous };
    if (text === null) delete next[project];
    else next[project] = text;
    return next;
  });
  const [{ projects, discards }, setDraft] = useState<{ projects: Record<string, ProjectEdits>; discards: Record<string, number> }>({ projects: {}, discards: {} });
  const edit: DraftContext["edit"] = (project, key, old, value) => setDraft((previous) => {
    const edits = { ...previous.projects[project] };
    if (same(old, value)) delete edits[key];
    else edits[key] = { old, value };
    return { ...previous, projects: { ...previous.projects, [project]: edits } };
  });
  const discard = (project: string) => setDraft((previous) => {
    const { [project]: _dropped, ...rest } = previous.projects;
    return { projects: rest, discards: { ...previous.discards, [project]: (previous.discards[project] ?? 0) + 1 } };
  });
  const saved: DraftContext["saved"] = (project, submitted) => setDraft((previous) => {
    const edits = { ...previous.projects[project] };
    const discarded = previous.discards[project] !== discards[project];
    for (const [key, sent] of Object.entries(submitted)) {
      if (!edits[key] && discarded) continue;
      const current = edits[key] ?? { old: sent.value, value: sent.old };
      if (same(current.value, sent.value)) delete edits[key];
      else edits[key] = { ...current, old: sent.value };
    }
    return { ...previous, projects: { ...previous.projects, [project]: edits } };
  });
  return createElement(Context.Provider, { value: { projects, edit, discard, saved, raw, editRaw } }, children);
}

export function useSettingsDraft() {
  const draft = useContext(Context);
  if (!draft) throw new Error("Settings draft requires its provider");
  return draft;
}
