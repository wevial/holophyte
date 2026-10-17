import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useContext } from "react";
import { actionBase, postAction } from "../lib/actions";
import { cancelTicket, moveTicket, type WriteResult } from "../lib/boardWrites";
import { putConfig, type ConfigWrite } from "../lib/config";
import { useToast } from "./components/Toast";
import { PollDepsContext } from "./scope";
import { routeKey } from "./queries";

export function useConfigSave() {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  return useMutation({
    mutationFn: (write: { base: string; revision: string } & ConfigWrite) =>
      putConfig(write.base, "text" in write ? { text: write.text } : { patch: write.patch }, deps.fetch, write.revision),
    onSettled: (_result, _error, write) => client.invalidateQueries({ queryKey: routeKey(write.base, "/config") }),
  });
}

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

/** One revision-checked board write to ticket `key` at `base`: a move to
 *  `column`, or a cancel with the reason `note`. */
export type BoardWrite = { base: string; key: string; revision: number; said: string } & ({ column: "ready" | "backlog" } | { note: string });

/** A move or cancel as a mutation resolving to the daemon's `WriteResult`.
 *  An `ok` answer says its sentence and, like a 409, re-reads the fleet and
 *  every route query of `base`; nothing is retried. */
export function useBoardWrite() {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (write: BoardWrite) =>
      "column" in write
        ? moveTicket(write.base, write.key, write.revision, write.column, deps.fetch)
        : cancelTicket(write.base, write.key, write.revision, write.note, deps.fetch),
    onSuccess: (result, write) => {
      if (result.ok) toast(write.said);
      if (!result.ok && !("conflict" in result)) return;
      void client.invalidateQueries({ queryKey: ["fleet"] });
      void client.invalidateQueries({ queryKey: ["route", write.base] });
    },
  });
}

/** Why `result` was refused, one line a problem, or null for a taken write. */
export function writeRefusal(result: WriteResult | undefined, key: string): string[] | null {
  if (result == null || result.ok) return null;
  if ("conflict" in result) return [`${key} changed since this page read it. It is read again; try once more.`];
  return "problems" in result ? result.problems : [result.error];
}
