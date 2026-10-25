import { expect, test } from "bun:test";
import { runDetailSchema } from "../src/lib/contract.gen";
import { ContractError, fetchJson } from "../src/lib/poll";
import detail from "../../tests/fixtures/serve/run-detail.json";

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
