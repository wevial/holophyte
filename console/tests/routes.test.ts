import { expect, test } from "bun:test";
import { routeParts } from "../src/lib/routes";

test("a route label reads as its harness and model, wrapper suffix dropped and gpt capitalised", () => {
  const labels = ["claude-implement opus", "codex-review gpt-6-astra", "codex gpt-6-astra", "devin-review", "claude sonnet", "my-tool x"];
  expect(labels.map(routeParts)).toEqual([
    { harness: "Claude", model: "Opus" },
    { harness: "Codex", model: "GPT-6 Astra" },
    { harness: "Codex", model: "GPT-6 Astra" },
    { harness: "Devin", model: "" },
    { harness: "Claude", model: "Sonnet" },
    { harness: "my-tool", model: "X" },
  ]);
});

test("a command named after an Object property reads as itself, not the inherited value", () => {
  expect(routeParts("__proto__ x")).toEqual({ harness: "__proto__", model: "X" });
  expect(routeParts("constructor-review")).toEqual({ harness: "constructor", model: "" });
});
