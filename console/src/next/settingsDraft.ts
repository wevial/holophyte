import { createContext, createElement, useContext, useState, type ReactNode } from "react";
import { same, type PatchValue } from "../lib/config";

export type SettingsEdit = { old: PatchValue; value: PatchValue };
type ProjectEdits = Record<string, SettingsEdit>;
type DraftContext = {
  projects: Record<string, ProjectEdits>;
  edit: (project: string, key: string, old: PatchValue, value: PatchValue) => void;
  discard: (project: string) => void;
  saved: (project: string, submitted: ProjectEdits) => void;
};
const Context = createContext<DraftContext | null>(null);

export function SettingsDraftProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Record<string, ProjectEdits>>({});
  const edit: DraftContext["edit"] = (project, key, old, value) => setProjects((previous) => {
    const edits = { ...previous[project] };
    if (same(old, value)) delete edits[key];
    else edits[key] = { old, value };
    return { ...previous, [project]: edits };
  });
  const discard = (project: string) => setProjects(({ [project]: _dropped, ...rest }) => rest);
  const saved: DraftContext["saved"] = (project, submitted) => setProjects((previous) => {
    const edits = { ...previous[project] };
    for (const [key, sent] of Object.entries(submitted)) {
      const current = edits[key] ?? { old: sent.value, value: sent.old };
      if (same(current.value, sent.value)) delete edits[key];
      else edits[key] = { ...current, old: sent.value };
    }
    return { ...previous, [project]: edits };
  });
  return createElement(Context.Provider, { value: { projects, edit, discard, saved } }, children);
}

export function useSettingsDraft() {
  const draft = useContext(Context);
  if (!draft) throw new Error("Settings draft requires its provider");
  return draft;
}
