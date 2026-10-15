import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useContext } from "react";
import { actionBase, postAction } from "../lib/actions";
import { useToast } from "./components/Toast";
import { PollDepsContext } from "./scope";

/** One post: `route` for `label` on the daemon whose project answers at
 *  `base`, and the sentence the toast says when the daemon takes it. */
interface ActionPost {
  label: string;
  base: string;
  route: string;
  body: Record<string, unknown>;
  said: string;
}

/** A console action as a mutation resolving to the daemon's `{ ok, detail }`.
 *  An `ok` answer re-reads the fleet and every route query of `base`, and
 *  says its sentence in the toast; a refusal changes nothing. */
export function useAction() {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (post: ActionPost) => postAction(actionBase(post.label, post.base), post.route, post.body, deps.fetch),
    onSuccess: (result, post) => {
      if (!result.ok) return;
      toast(post.said);
      void client.invalidateQueries({ queryKey: ["fleet"] });
      void client.invalidateQueries({ queryKey: ["route", post.base] });
    },
  });
}
