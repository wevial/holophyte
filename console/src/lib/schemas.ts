import { z } from "zod";

// All wire objects pass through unknown keys: newer daemons may add fields.
export const supervisorSchema = z.looseObject({
  state: z.enum(["live", "stale", "none"]),
  pid: z.number().nullable(), heartbeat_age_ms: z.number().nullable(), host: z.string().nullable(),
});

export const runSchema = z.looseObject({
  id: z.number(), ticket: z.string(), phase: z.string(),
  ticket_url: z.string().nullable().optional(), pr_url: z.string().nullable().optional(),
  heartbeat_age_ms: z.number(), elapsed_ms: z.number(),
  working_ms: z.number().nullable().optional(), work_started_ms: z.number().nullable().optional(),
  time_box_ms: z.number().nullable(), host: z.string().nullable(), title: z.string().nullable().optional(),
  started_ms: z.number().optional(), round: z.number().optional(), strikes: z.number().optional(),
});

export const statusSchema = z.looseObject({
  target: z.string(), project: z.string().optional(), schema_version: z.number().optional(),
  host: z.string(), now: z.number(),
  daemon: z.looseObject({ started_ms: z.number(), pid: z.number() }).optional(),
  workers_on_previous_build: z.number().optional(),
  active_routes: z.record(z.string(), z.looseObject({
    command: z.string().nullable(), fallback: z.string().optional(),
  })).optional(),
  supervisor: supervisorSchema,
  thresholds: z.looseObject({ heartbeat_stale_ms: z.number(), strikes: z.number() }),
  actions: z.boolean().optional(), config_edit: z.boolean().optional(),
  runs: z.array(runSchema),
});

export const threadFindingFieldsSchema = z.looseObject({
  fingerprint: z.looseObject({
    path: z.string(), line: z.number().nullable().optional(), severity: z.string(),
  }).optional(),
  kind: z.enum(["thread", "finding"]).optional(), author: z.string().optional(),
  author_kind: z.string().optional(), verdict: z.string().optional(),
  summary: z.string().optional(), raw: z.string().optional(), url: z.string().optional(),
});
export const findingSchema = threadFindingFieldsSchema.extend({
  path: z.string(), line: z.number().nullable().optional(), severity: z.string(),
  criterion: z.string().nullable().optional(), message: z.string(),
});
export const instructionSchema = z.looseObject({
  kind: z.literal("instruction"), path: z.string(), line: z.number().nullable().optional(),
  author: z.string(), request: z.string(), url: z.string(),
  triage: z.object({
    decision: z.enum(["fix", "question", "unclear"]), confidence: z.number().nullable(),
    route: z.enum(["fix", "answer"]), reason: z.string(),
  }).optional(),
  outcome: z.enum(["changed", "kept", "asked"]).optional(), reply: z.string().optional(),
});
export const operatorNoteSchema = z.looseObject({
  kind: z.literal("operator_note"), event_id: z.number(), note: z.string(), author: z.string(),
});
export const roundSchema = z.looseObject({
  round: z.number(), started_ms: z.number(), ended_ms: z.number().nullable(),
  verdict: z.string(), reviewer_model: z.string().nullable().optional(),
  findings: z.array(findingSchema), instructions: z.array(instructionSchema).optional(),
  operator_notes: z.array(operatorNoteSchema).optional(),
});
export const runEventSchema = z.looseObject({ at: z.number(), kind: z.string(), summary: z.string() });
export const runDetailSchema = z.looseObject({
  run: z.looseObject({
    id: z.number(), ticket: z.string(), ticket_url: z.string().nullable().optional(),
    title: z.string().nullable().optional(), phase: z.string(), attempt: z.number().optional(),
    started_ms: z.number(), ended_ms: z.number().nullable(), elapsed_ms: z.number().optional(),
    working_ms: z.number().nullable().optional(), work_started_ms: z.number().nullable().optional(),
    outcome: z.string().nullable().optional(), time_box_ms: z.number().nullable(),
    branch: z.string().nullable().optional(), host: z.string().nullable(),
    heartbeat_age_ms: z.number().nullable().optional(), merge_sha: z.string().nullable().optional(),
    commit_url: z.string().nullable().optional(), pr_url: z.string().nullable().optional(),
    max_rounds: z.number().optional(), approved_at: z.number().nullable().optional(),
    approved_by: z.string().nullable().optional(),
  }),
  findings: z.array(z.looseObject({ tone: z.literal("advisory"), message: z.string() })).optional(),
  rounds: z.array(roundSchema), events: z.array(runEventSchema),
});
