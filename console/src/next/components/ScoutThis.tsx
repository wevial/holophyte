import { useNavigate } from "@tanstack/react-router";
import { scoutThis, type ScoutTarget } from "../scouts";
import { Btn } from "./Btn";

/** "Scout": opens the Scouts page's New scout form filled for `target`. */
export function ScoutThis({ target }: { target: ScoutTarget }) {
  const navigate = useNavigate();
  return (
    <Btn disabled={target.off != null} title={target.off ?? undefined} onClick={() => void navigate(scoutThis(target))}>
      Scout
    </Btn>
  );
}
