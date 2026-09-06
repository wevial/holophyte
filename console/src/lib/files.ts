import type { RunFilesBody, TouchedFile } from "./types";

/** Rows shown before "Show all N files" is needed. */
export const FILES_CAP = 6;

export type StatusLetter = "M" | "A" | "D" | "R";

/** The daemon's status letter for the row: `M`, `A`, `D` or `R`. A rename
 *  arrives as `R` (the daemon drops git's score) but a raw `R100` still
 *  reads as one; anything unknown is shown as a modification. */
export function statusLetter(status: string): StatusLetter {
  const letter = status.trim().charAt(0).toUpperCase();
  return letter === "A" || letter === "D" || letter === "R" ? letter : "M";
}

/** The rows to render: all of them when `showAll`, else the first `FILES_CAP`. */
export function capFiles<T extends TouchedFile>(files: T[], showAll: boolean): T[] {
  return showAll || files.length <= FILES_CAP ? files : files.slice(0, FILES_CAP);
}

/** The label's count: `12 · +156 −86`. */
export function filesLabel(body: Pick<RunFilesBody, "files" | "total_added" | "total_deleted">): string {
  return `${body.files.length} · +${body.total_added} −${body.total_deleted}`;
}
