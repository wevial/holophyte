import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { TicketLink } from "../src/components/TicketLink";

afterEach(cleanup);

test("ticket URL opens externally without toggling its parent", () => {
  let toggles = 0;
  render(<div onClick={() => toggles++}><TicketLink ticket="KO-478" ticket_url="https://linear.app/team/issue/KO-478" /></div>);
  const link = screen.getByRole("link", { name: "KO-478" });
  expect(link.getAttribute("href")).toBe("https://linear.app/team/issue/KO-478");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  fireEvent.click(link);
  expect(toggles).toBe(0);
});

test("missing URL preserves the identifier as text", () => {
  const { rerender } = render(<TicketLink ticket="KO-478" />);
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.getByText("KO-478")).toBeTruthy();
  rerender(<TicketLink ticket="KO-478" ticket_url={null} />);
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.getByText("KO-478")).toBeTruthy();
});
