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
