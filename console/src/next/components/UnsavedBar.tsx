import { Btn } from "./Btn";
import { useSettingsDraft } from "../settingsDraft";

export function UnsavedBar({ project, onSave, onDiscard, disabled = false, invalid = false }: { project: string; onSave: () => void; onDiscard: () => void; disabled?: boolean; invalid?: boolean }) {
  const { projects, discard } = useSettingsDraft();
  const edits = Object.entries(projects[project] ?? {});
  if (!edits.length) return null;
  return (
    <section aria-label="Unsaved changes" className="mt-4 flex flex-wrap items-center justify-between gap-4 border border-act bg-paper px-5 py-4">
      <p className="min-w-0 break-words font-mono text-[12px] text-ink-2">
        <span className="text-act">{edits.length}</span> unsaved {edits.length === 1 ? "change" : "changes"}
        {edits.map(([key, edit]) => <span key={key}> · {key.split(".").pop()} {String(edit.old)} → {String(edit.value)}</span>)}
        {invalid && " · fix the error first"}
      </p>
      <div className="flex items-center gap-2">
        <button type="button" onClick={() => { discard(project); onDiscard(); }} className="min-h-9 cursor-pointer px-3 font-mono text-[12px] text-ink-2 hover:text-ink">Discard</button>
        <Btn variant="primary" disabled={disabled} onClick={onSave}>Review and save</Btn>
      </div>
    </section>
  );
}
