import { useNavigate } from "@tanstack/react-router";
import { projectName } from "../../lib/derive";
import type { ScopeSearch } from "../scope";
import type { ScoutTarget } from "../scouts";
import { Btn } from "./Btn";

/** "Scout": opens the Scouts page's New scout form filled for `target`, keeping the scope. */
export function ScoutThis({ target }: { target: ScoutTarget }) {
  const navigate = useNavigate();
  const { project, about, kind, ask } = target;
  const open = () => void navigate({ to: "/scouts", search: (previous: ScopeSearch) => ({ project: previous.project, new: projectName(project), about, kind, ask }) });
  return (
    <Btn disabled={target.off != null} title={target.off ?? undefined} onClick={open}>
      Scout
    </Btn>
  );
}
