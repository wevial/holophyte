import { expect, test } from "bun:test";
import { render, screen } from "@testing-library/react";
import { App } from "../src/App";

test("the placeholder page shows the product name", () => {
  render(<App />);
  expect(screen.getByText("Holophyte")).toBeTruthy();
});
