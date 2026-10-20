import { afterEach, expect, test } from "bun:test";
import { cleanup, render } from "@testing-library/react";
import { PrFacts } from "../../src/components/PrFacts";
import type { Fact } from "../../src/lib/attention";

afterEach(cleanup);

test("line facts identify all four tones with distinct hidden SVG paths instead of squares", () => {
  const facts: Fact[] = [
    { label: "checks green", tone: "ok" },
    { label: "review pending", tone: "warn" },
    { label: "checks failing", tone: "bad" },
    { label: "checks unknown", tone: "neutral" },
  ];
  const { container } = render(<PrFacts facts={facts} form="line" />);
  const rendered = container.querySelectorAll("[data-fact]");
  expect(rendered.length).toBe(4);
  expect(container.querySelectorAll("i").length).toBe(0);
  const paths = new Set<string>();
  for (const [index, tone] of ["ok", "warn", "bad", "neutral"].entries()) {
    const glyphs = rendered[index]!.querySelectorAll("svg");
    expect(glyphs.length).toBe(1);
    const glyph = glyphs[0]!;
    expect(glyph.getAttribute("data-glyph")).toBe(tone);
    expect(glyph.getAttribute("aria-hidden")).toBe("true");
    const path = glyph.querySelector("path")?.getAttribute("d");
    expect(path).toBeTruthy();
    paths.add(path!);
  }
  expect(paths.size).toBe(4);
});

test("plain facts are bare labels with no glyph or separator, and the line form keeps a glyph per fact", () => {
  const facts: Fact[] = [
    { label: "checks ✓", tone: "ok" },
    { label: "waits on KO-3", tone: "warn" },
    { label: "checks failing", tone: "bad" },
    { label: "13 files +718 −85", tone: "neutral" },
  ];
  const plain = render(<PrFacts facts={facts} form="plain" />).container;
  const line = plain.querySelector("[data-facts]")!;
  expect([...line.querySelectorAll("[data-fact]")].map((fact) => fact.innerHTML)).toEqual(["checks ✓", "waits on KO-3", "checks failing", "13 files +718 −85"]);
  expect(line.querySelectorAll("svg").length).toBe(0);
  expect(line.textContent).not.toContain("·");
  cleanup();
  const glyphed = render(<PrFacts facts={facts} form="line" />).container;
  expect([...glyphed.querySelectorAll("[data-fact]")].map((fact) => fact.querySelectorAll("svg").length)).toEqual([1, 1, 1, 1]);
});
