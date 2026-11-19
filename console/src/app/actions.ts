import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useContext } from "react";
import { actionBase, postAction, type ActionResult } from "../lib/actions";
import type { Fetch } from "../lib/poll";
import { cancelTicket, moveTicket, type WriteResult } from "../lib/boardWrites";
import { putConfig, type ConfigWrite } from "../lib/config";
import { useToast } from "./components/Toast";
import type { PrOpenItem } from "../lib/contract.gen";
import { patchFleetItem } from "./fleet";
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
 *  `base`, and the sentence the toast says when the daemon takes it, or
 *  makes of the daemon's `detail`. */
interface ActionPost {
  label: string;
  base: string;
  route: string;
  body: Record<string, unknown>;
  said: string | ((detail: string, result: ActionResult) => string);
  /** What an `ok` answer makes of the cached `pr_open` item of a run. */
  patch?: { run: number; change: (item: PrOpenItem) => PrOpenItem };
}

/** A console action as a mutation resolving to the daemon's `{ ok, detail }`.
 *  An `ok` answer applies the post's patch, re-reads the fleet and every
 *  route query of `base`, and says its sentence in the toast; a refusal
 *  changes nothing. */
export function useAction() {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (post: ActionPost) => postAction(actionBase(post.label, post.base), post.route, post.body, deps.fetch),
    onSuccess: (result, post) => {
      if (!result.ok) return;
      toast(typeof post.said === "string" ? post.said : post.said(result.detail, result));
      if (post.patch) patchFleetItem(client, post.base, post.patch.run, post.patch.change);
      void client.invalidateQueries({ queryKey: ["fleet"] });
      void client.invalidateQueries({ queryKey: ["route", post.base] });
    },
  });
}

/** One revision-checked board write to ticket `key` at `base`: a move to
 *  `column`, or a cancel with the reason `note`. */
export type BoardWrite = { base: string; key: string; revision: number; said: string } & ({ column: "ready" | "backlog" } | { note: string });

/** The mutation key every board write to ticket `key` at `base` shares, so
 *  each control drawing that ticket's moves sees one in flight. */
export const boardWriteKey = (base: string, key: string) => ["board-write", base, key] as const;

/** A move or cancel as a mutation resolving to the daemon's `WriteResult`.
 *  An `ok` answer says its sentence and, like a 409, re-reads the fleet and
 *  every route query of `base`; nothing is retried. */
export function useBoardWrite(mutationKey?: readonly unknown[]) {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationKey,
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

/** One later merge that changed a file the reverted merge changed. */
interface RevertDependent {
  key: string;
  sha: string;
  paths: string[];
}

/** What `POST /actions/revert` answered: a preview's dependents, the filed
 *  ticket's key, a check's refusal with the refused paths, or a request
 *  that failed for another reason. */
export type RevertAnswer =
  | { kind: "preview"; dependents: RevertDependent[] }
  | { kind: "filed"; ticket: string }
  | { kind: "refused"; reason: string; paths: string[] }
  | { kind: "failed"; reason: string };

/** The daemon wants a reason on a preview too; it records none for it. */
const PREVIEW_REASON = "preview";

const strings = (value: unknown): string[] => (Array.isArray(value) ? value.filter((each): each is string => typeof each === "string") : []);

function dependentsOf(value: unknown): RevertDependent[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((each: Record<string, unknown> | null) =>
    each != null && typeof each.key === "string" ? [{ key: each.key, sha: typeof each.sha === "string" ? each.sha : "", paths: strings(each.paths) }] : [],
  );
}

/** `POST /actions/revert` through `postAction()`, decoding the reply it
 *  hands back from the fetch it was given: `postAction()` keeps only `ok`
 *  and `detail`, and a revert's reply carries neither. */
async function postRevert(base: string, body: Record<string, unknown>, fetchImpl: Fetch): Promise<RevertAnswer> {
  let reply: Response | null = null;
  const keep: Fetch = async (url, init) => {
    const response = await fetchImpl(url, init);
    reply = response.clone();
    return response;
  };
  const posted = await postAction(base, "/actions/revert", body, keep);
  const answered = reply as Response | null;
  if (answered == null) return { kind: "failed", reason: posted.detail };
  const answer: unknown = await answered.json().catch(() => null);
  const record = typeof answer === "object" && answer != null ? (answer as Record<string, unknown>) : {};
  const refused = typeof record.refused === "object" && record.refused != null ? (record.refused as Record<string, unknown>) : null;
  if (refused != null && typeof refused.reason === "string") return { kind: "refused", reason: refused.reason, paths: strings(refused.paths) };
  if (!answered.ok) return { kind: "failed", reason: posted.detail };
  if (answer !== record) return { kind: "failed", reason: "the daemon's reply could not be read" };
  if (body.dry_run === true) return { kind: "preview", dependents: dependentsOf(record.dependents) };
  return typeof record.key === "string" ? { kind: "filed", ticket: record.key } : { kind: "failed", reason: "the daemon named no revert ticket" };
}

/** The `dry_run` preview of reverting `key` at `base`; it files nothing. */
export function previewRevert(base: string, key: string, fetchImpl: Fetch): Promise<RevertAnswer> {
  return postRevert(base, { key, reason: PREVIEW_REASON, dry_run: true }, fetchImpl);
}

/** Filing the revert of `key` at `base` for `reason` as a mutation; a filed
 *  revert re-reads the fleet and `base`'s route queries. */
export function useRevert() {
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ base, key, reason }: { base: string; key: string; reason: string }) => postRevert(base, { key, reason }, deps.fetch),
    onSuccess: (answer, { base }) => {
      if (answer.kind !== "filed") return;
      void client.invalidateQueries({ queryKey: ["fleet"] });
      void client.invalidateQueries({ queryKey: ["route", base] });
    },
  });
}
