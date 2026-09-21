import { expect, test } from "bun:test";
import { ContractError, fetchJson, pollOnce } from "../src/lib/poll";
import { runDetailSchema, statusSchema } from "../src/lib/schemas";
import status from "../../tests/fixtures/serve/status.json";
import detail from "../../tests/fixtures/serve/run-detail.json";

test("both shared daemon fixtures parse, including newer nested fields", () => {
  expect<unknown>(statusSchema.parse(status)).toEqual(status);
  expect<unknown>(runDetailSchema.parse(detail)).toEqual(detail);
});

test("fetch boundary rejects missing and mistyped fields and allows additions", async () => {
  const endpoint = "http://writer:7710/runs/1";
  const missing = structuredClone(detail) as Record<string, unknown>;
  delete missing.rounds;
  for (const [body, path] of [
    [missing, "rounds"],
    [{ ...detail, run: { ...detail.run, phase: 42 } }, "run.phase"],
  ] as const) {
    try {
      await fetchJson(async () => Response.json(body), endpoint, runDetailSchema);
      throw new Error("invalid response was accepted");
    } catch (error) {
      expect(error).toBeInstanceOf(ContractError);
      expect((error as ContractError).endpoint).toBe(endpoint);
      expect((error as ContractError).path).toBe(path);
    }
  }
  const newer = { ...detail, extra: true, run: { ...detail.run, extra: "new" } };
  expect<unknown>(await fetchJson(async () => Response.json(newer), endpoint, runDetailSchema)).toEqual(newer);
});

test("status polling validates nested run fields at the boundary", async () => {
  const body = { ...status, runs: [{ ...status.runs[0], phase: false }] };
  await expect(pollOnce("http://writer:7710", async (url) =>
    Response.json(url.endsWith("/status") ? body : { items: [] })
  )).rejects.toThrow("http://writer:7710/status at runs.0.phase");
});
