import { afterEach, expect, test } from "bun:test";
import { cleanup, render } from "@testing-library/react";
import { TitleText } from "../../src/app/components/TitleText";

afterEach(cleanup);

const draw = (title: string) => render(<p><TitleText>{title}</TitleText></p>).container.firstElementChild as HTMLElement;
const codes = (node: HTMLElement) => [...node.querySelectorAll("code")].map((code) => code.textContent);

test("a backticked span renders as one code element with the text around it unchanged and no backtick shown", () => {
  const title = draw("A ticket file can open with a `+++` TOML header");
  expect(codes(title)).toEqual(["+++"]);
  expect(title.textContent).toBe("A ticket file can open with a +++ TOML header");
});

test("each paired span is its own code element, and an unpaired backtick stays a literal character", () => {
  expect(codes(draw("two `a` and `b` spans"))).toEqual(["a", "b"]);
  const stray = draw("one stray ` backtick");
  expect(codes(stray)).toEqual([]);
  expect(stray.textContent).toBe("one stray ` backtick");
});

test("markup inside a span shows as literal characters in its code element and adds no element", () => {
  const title = draw("a `<b>x</b>` tag");
  expect(codes(title)).toEqual(["<b>x</b>"]);
  expect(document.querySelector("b")).toBeNull();
});
