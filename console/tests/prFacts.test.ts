import { expect, test } from "bun:test";
import { prFacts, type Fact } from "../src/lib/attention";

const labels = (facts: Fact[]) => facts.map((fact) => [fact.label, fact.tone]);

test("green checks, an approval and no threads are three ok chips", () => {
  expect(labels(prFacts({ number: 2170, checks: "success", review: "approved", threads: 0 }))).toEqual([
    ["checks green", "ok"],
    ["approved", "ok"],
    ["no open threads", "ok"],
  ]);
});

test("pending checks, changes requested and open threads read warn, bad and warn", () => {
  expect(labels(prFacts({ number: 2170, checks: "pending", review: "changes_requested", threads: 2 }))).toEqual([
    ["checks pending", "warn"],
    ["changes requested", "bad"],
    ["2 threads open", "warn"],
  ]);
});

test("failing checks are bad, a review still required is pending, one thread is singular", () => {
  expect(labels(prFacts({ number: 2170, checks: "failure", review: "review_required", threads: 1 }))).toEqual([
    ["checks failing", "bad"],
    ["review pending", "warn"],
    ["1 thread open", "warn"],
  ]);
});

test("facts the daemon never polled each read unknown as a neutral chip, not nothing", () => {
  expect(labels(prFacts({ number: 2170, checks: null, review: null, threads: null }))).toEqual([
    ["checks unknown", "neutral"],
    ["review unknown", "neutral"],
    ["threads unknown", "neutral"],
  ]);
  expect(labels(prFacts({}))).toEqual([
    ["checks unknown", "neutral"],
    ["review unknown", "neutral"],
    ["threads unknown", "neutral"],
  ]);
});
