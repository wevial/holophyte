import { useState } from "react";
import { capFiles, filesLabel, statusLetter, type StatusLetter } from "../lib/files";
import type { RunFilesBody } from "../lib/types";

/** The daemon's two named refusals (404 unknown run, 409 no range to diff)
 *  mean the rows are gone for good, so they replace a body kept from an
 *  earlier poll and are the only failures the column names, in the
 *  endpoint's own words; any other failure leaves the last body, or reads
 *  as no files yet when there is none. */
const REFUSALS: ReadonlySet<number> = new Set([404, 409]);

const LETTER_TONE: Record<StatusLetter, string> = {
  M: "text-muted",
  A: "text-ok-text",
  D: "text-bad-text",
  R: "text-muted",
};

/** The detail's right column: the paths the run touched with their line
 *  counts from `/runs/N/files`, six rows until "Show all N files". Without
 *  rows it is one line: the refusal's message for a 404 or 409, `loading…`
 *  before the first answer, else `No files yet`. */
export function FilesTouched({
  files,
  error,
  status,
  loading,
}: {
  files: RunFilesBody | null;
  error: string | null;
  /** The HTTP status behind `error`, null for a failure with no answer. */
  status: number | null;
  loading: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const refused = error !== null && status !== null && REFUSALS.has(status);
  const body = refused ? null : files;
  return (
    <section data-files aria-label="Files touched" className="min-w-0">
      <div className="flex items-baseline gap-3">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">Files touched</span>
        {body && (
          <span data-files-label className="font-mono text-[12px] text-muted">
            {filesLabel(body)}
          </span>
        )}
      </div>
      {body ? (
        <Rows body={body} showAll={showAll} onToggle={() => setShowAll((previous) => !previous)} />
      ) : (
        <p data-files-note className={`mt-2 text-[12px] ${refused ? "font-semibold text-bad" : "text-muted"}`}>
          {refused ? error : loading ? "loading…" : "No files yet"}
        </p>
      )}
    </section>
  );
}

function Rows({ body, showAll, onToggle }: { body: RunFilesBody; showAll: boolean; onToggle: () => void }) {
  const { files } = body;
  if (files.length === 0) {
    return (
      <p data-files-note className="mt-2 text-[12px] text-muted">
        No files yet
      </p>
    );
  }
  const shown = capFiles(files, showAll);
  const capped = shown.length < files.length || showAll;
  return (
    <>
      <ul className="mt-2 rounded-card bg-list-box px-3 py-2 font-mono text-[12px]">
        {shown.map((file) => {
          const letter = statusLetter(file.status);
          return (
            <li
              key={file.path}
              data-file
              data-status={letter}
              className="grid grid-cols-[14px_1fr_auto] items-baseline gap-x-2 py-[2px]"
            >
              <span data-letter className={`font-semibold ${LETTER_TONE[letter]}`}>
                {letter}
              </span>
              <span data-path className="truncate text-ink" title={file.path}>
                {file.path}
              </span>
              <span className="whitespace-nowrap">
                <span data-added className="text-ok-text">
                  +{file.added}
                </span>{" "}
                <span data-deleted className="text-bad-text">
                  −{file.deleted}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
      {capped && (
        <button
          type="button"
          data-files-toggle
          onClick={onToggle}
          className="mt-2 text-[12px] font-semibold text-link"
        >
          {showAll ? "Show fewer" : `Show all ${files.length} files`}
        </button>
      )}
    </>
  );
}
