import { z } from "zod";
import { AnswerError, fetchJson, type Fetch } from "../lib/poll";
import { useRunResource } from "./useRunResource";

export const turnsSchema = z.object({ turns: z.array(z.object({
  id: z.number().int(),
  role: z.enum(["implement", "review", "adjudicate", "write"]),
  label: z.string().nullable().optional(),
  route: z.enum(["primary", "fallback"]),
  seconds: z.number().nullable(),
  session_id: z.string().nullable(),
}).strict()) }).strict();

export type RunTurnsBody = z.infer<typeof turnsSchema>;

export const transcriptSchema = z.object({ entries: z.array(z.object({
  speaker: z.enum(["user", "assistant", "command", "tool"]),
  text: z.string(),
}).strict()) }).strict();

export function useRunTurns(base: string, id: number, polls: number, deps?: { fetch: Fetch }) {
  return useRunResource(base, id, polls,
    (base, id, fetch) => fetchJson(fetch, `${base}/runs/${id}/turns`, turnsSchema), deps);
}

export function useTurnTranscript(base: string, runId: number, turnId: number | null, deps?: { fetch: Fetch }) {
  return useRunResource(`${base}/runs/${runId}/turns`, turnId, 0,
    async (base, id, fetch) => {
      try {
        return await fetchJson(fetch, `${base}/${id}/transcript`, transcriptSchema);
      } catch (error) {
        if (error instanceof AnswerError && error.status === 404) {
          throw new Error("Transcript unavailable. The daemon needs an allowed root and a retained session file.");
        }
        throw error;
      }
    }, deps);
}
