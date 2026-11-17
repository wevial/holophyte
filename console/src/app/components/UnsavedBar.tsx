import { useSettingsDraft } from "../settingsDraft";

export function UnsavedBar({ project, onSave, onDiscard, disabled = false, invalid = false }: { project: string; onSave: () => void; onDiscard: () => void; disabled?: boolean; invalid?: boolean }) {
  const { projects, discard } = useSettingsDraft();
  const count = Object.keys(projects[project] ?? {}).length;
  if (!count) return null;
  return (
    <section aria-label="Unsaved changes" className="sticky bottom-[22px] z-20 mx-auto mt-auto flex h-11 w-fit max-w-full items-center gap-1.5 rounded-xl bg-raised pr-1.5 pl-4 font-sans text-[13px] shadow-[0_0_0_1px_var(--hair-2),var(--card-shadow)]">
      <p className="mr-2 min-w-0 text-ink-2">{count} {count === 1 ? "change" : "changes"}{invalid && " · fix the error first"}</p>
      <button type="button" onClick={() => { discard(project); onDiscard(); }} className="inline-flex h-8 cursor-pointer items-center rounded-lg px-3.5 font-medium text-ink-2 hover:bg-hover hover:text-ink">Discard</button>
      <button type="button" disabled={disabled} onClick={onSave} className="inline-flex h-8 cursor-pointer items-center rounded-lg bg-ink px-3.5 font-medium text-canvas disabled:cursor-not-allowed disabled:opacity-50">Save</button>
    </section>
  );
}
