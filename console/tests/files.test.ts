import { expect, test } from "bun:test";
import { FILES_CAP, capFiles, filesLabel, statusLetter } from "../src/lib/files";

test("statusLetter keeps M, A, D and R, reads a scored rename as R, and shows anything else as M", () => {
  expect(["M", "A", "D", "R"].map(statusLetter)).toEqual(["M", "A", "D", "R"]);
  expect(statusLetter("R100")).toBe("R");
  expect(statusLetter("T")).toBe("M");
  expect(statusLetter("")).toBe("M");
});

test("capFiles shows six until asked for all; a short list is never cut", () => {
  const files = Array.from({ length: 12 }, (_, i) => ({ path: `f${i}.py`, status: "M", added: 1, deleted: 0 }));
  expect(capFiles(files, false).map((f) => f.path)).toEqual(files.slice(0, FILES_CAP).map((f) => f.path));
  expect(capFiles(files, true)).toEqual(files);
  expect(capFiles(files.slice(0, 4), false)).toEqual(files.slice(0, 4));
});

test("filesLabel reads count · +added −deleted with the real minus sign", () => {
  const files = Array.from({ length: 12 }, (_, i) => ({ path: `f${i}.py`, status: "M", added: 13, deleted: 7 }));
  expect(filesLabel({ files, total_added: 156, total_deleted: 86 })).toBe("12 · +156 −86");
});
