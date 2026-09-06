import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FilesTouched } from "../src/components/FilesTouched";
import type { RunFilesBody } from "../src/lib/types";

/** Twelve files as `/runs/N/files` serves them, totals +156 −86 over the whole diff. */
const TWELVE: RunFilesBody = {
  files: [
    { path: "holophyte/serve.py", status: "M", added: 40, deleted: 12 },
    { path: "holophyte/files.py", status: "A", added: 60, deleted: 0 },
    { path: "holophyte/old_files.py", status: "D", added: 0, deleted: 30 },
    { path: "holophyte/runs.py", status: "R", added: 3, deleted: 3 },
    { path: "tests/test_serve.py", status: "M", added: 20, deleted: 10 },
    { path: "tests/test_files.py", status: "A", added: 18, deleted: 0 },
    { path: "console/src/App.tsx", status: "M", added: 2, deleted: 2 },
    { path: "console/src/lib/types.ts", status: "M", added: 4, deleted: 1 },
    { path: "console/src/components/RunDetail.tsx", status: "M", added: 3, deleted: 3 },
    { path: "console/tests/RunDetail.test.tsx", status: "M", added: 2, deleted: 9 },
    { path: "CLAUDE.md", status: "M", added: 2, deleted: 8 },
    { path: "FINDINGS.md", status: "M", added: 2, deleted: 8 },
  ],
  total_added: 156,
  total_deleted: 86,
};

afterEach(cleanup);

const rows = () => Array.from(document.querySelectorAll("[data-file]")) as HTMLElement[];

test("twelve files read 12 · +156 −86, show six, and Show all 12 files reveals them all then reads Show fewer", () => {
  render(<FilesTouched files={TWELVE} error={null} loading={false} />);
  expect(document.querySelector("[data-files-label]")!.textContent).toBe("12 · +156 −86");
  expect(rows().length).toBe(6);
  expect(rows().map((row) => row.querySelector("[data-path]")!.textContent)).toEqual(
    TWELVE.files.slice(0, 6).map((file) => file.path),
  );
  const toggle = screen.getByRole("button", { name: "Show all 12 files" });
  fireEvent.click(toggle);
  expect(rows().length).toBe(12);
  expect(toggle.textContent).toBe("Show fewer");
  fireEvent.click(toggle);
  expect(rows().length).toBe(6);
  expect(toggle.textContent).toBe("Show all 12 files");
});

test("rows carry the status letter in its tone: M grey, A green, D red, R grey; adds green, deletes red", () => {
  render(<FilesTouched files={TWELVE} error={null} loading={false} />);
  const first = rows().slice(0, 4);
  expect(first.map((row) => row.querySelector("[data-letter]")!.textContent)).toEqual(["M", "A", "D", "R"]);
  const tones = first.map((row) => row.querySelector("[data-letter]")!.className);
  expect(tones[0]).toContain("text-muted");
  expect(tones[1]).toContain("text-ok-text");
  expect(tones[2]).toContain("text-bad-text");
  expect(tones[3]).toContain("text-muted");
  const added = first[0]!.querySelector("[data-added]")!;
  const deleted = first[0]!.querySelector("[data-deleted]")!;
  expect([added.textContent, deleted.textContent]).toEqual(["+40", "−12"]);
  expect(added.className).toContain("text-ok-text");
  expect(deleted.className).toContain("text-bad-text");
});

test("a short list has no toggle, an empty one says No files yet, and a refusal is the one line given", () => {
  const { unmount } = render(
    <FilesTouched files={{ ...TWELVE, files: TWELVE.files.slice(0, 3) }} error={null} loading={false} />,
  );
  expect(rows().length).toBe(3);
  expect(document.querySelector("[data-files-toggle]")).toBeNull();
  unmount();
  render(<FilesTouched files={{ files: [], total_added: 0, total_deleted: 0 }} error={null} loading={false} />);
  expect(document.querySelector("[data-files-note]")!.textContent).toBe("No files yet");
  cleanup();
  render(<FilesTouched files={null} error="branch no longer on disk" loading={false} />);
  expect(document.querySelector("[data-files-note]")!.textContent).toBe("branch no longer on disk");
  expect(document.querySelector("[data-file]")).toBeNull();
});
