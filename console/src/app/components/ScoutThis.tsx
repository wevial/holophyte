import { useNavigate } from "@tanstack/react-router";
import { projectName } from "../../lib/derive";
import type { ScopeSearch } from "../scope";
import type { ScoutTarget } from "../scouts";
import { Btn } from "./Btn";

/** Opens the Scouts page's New scout form filled for `target`, scoped to its project unless unscoped. */
export function useScoutThis(target: ScoutTarget): () => void {
  const navigate = useNavigate();
  const { project, about, kind, ask } = target;
  const name = projectName(project);
  return () => void navigate({ to: "/scouts", search: (previous: ScopeSearch) => ({ project: previous.project == null ? undefined : name, new: name, about, kind, ask }) });
}

/** "Scout": `useScoutThis()` as a button. */
export function ScoutThis({ target }: { target: ScoutTarget }) {
  const open = useScoutThis(target);
  return (
    <Btn disabled={target.off != null} title={target.off ?? undefined} onClick={open}>
      Scout
    </Btn>
  );
}
