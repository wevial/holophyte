import { columns } from "../lib/board";
import type { HostRecord } from "../lib/hosts";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { groupByDay, medianRounds, withinDays } from "../lib/shipped";
import { useBoard } from "../hooks/useBoard";
import type { ShippedState } from "../hooks/useShipped";
import { BoardColumn } from "./BoardColumn";
import { ShippedTable } from "./ShippedTable";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/**
 * The Board view: every host's `/board` as five columns, left to right
 * the path to merge, each card joined with the host's live run or open
 * question; beneath them today's merges from `shipped`, the `/shipped`
 * ledger the shell holds for the Shipped view and this table alike. `now`
 * is the clock naming today; `tz` pins the zone for tests.
 */
export function Board({
  hosts,
  shipped,
  now,
  polls = 0,
  deps = defaultPollDeps,
  tz,
}: {
  hosts: HostRecord[];
  shipped: ShippedState;
  now: number;
  polls?: number;
  deps?: { fetch: Fetch };
  tz?: string;
}) {
  const board = useBoard(hosts, polls, deps);
  const grouped = columns(board.cards);
  const today = withinDays(groupByDay(shipped.rows, now, tz), 1);
  const todayRows = today.flatMap((group) => group.rows);
  const median = medianRounds(todayRows);
  return (
    <section aria-label="Board" className="px-6 pt-6 pb-6">
      <div className="flex items-baseline gap-3">
        <h1 className="text-[20px] font-semibold text-ink">Board</h1>
        <span data-subtitle className="text-[13px] text-muted">
          {plural(board.cards.length, "open ticket")} · left to right is the path to merge
        </span>
        <span className="ml-auto text-[12px] text-faint">drag to reorder the ready column later</span>
      </div>
      {board.errors.map((error) => (
        <p key={error} role="alert" className="mt-2 font-mono text-[11px] text-bad-text">
          board failed: {error}
        </p>
      ))}
      <div className="mt-4 grid grid-cols-[repeat(5,minmax(0,1fr))] gap-3">
        {grouped.map((column) => (
          <BoardColumn key={column.state} column={column} />
        ))}
      </div>
      <section aria-label="Shipped today" className="mt-6">
        <div className="flex items-baseline gap-3">
          <h2 className="text-[15px] font-semibold text-ink">Shipped today</h2>
          <span data-shipped-subtitle className="text-[12px] text-muted">
            {plural(todayRows.length, "merge")}
            {median == null ? "" : ` · median ${median} rounds`}
          </span>
        </div>
        {shipped.errors.map((error) => (
          <p key={error} role="alert" className="mt-2 font-mono text-[11px] text-bad-text">
            shipped failed: {error}
          </p>
        ))}
        {todayRows.length === 0 ? (
          <p className="mt-3 text-[13px] text-muted">{shipped.loading ? "" : "Nothing merged today"}</p>
        ) : (
          <div className="mt-3">
            <ShippedTable rows={shipped.rows} now={now} tz={tz} days={1} />
          </div>
        )}
      </section>
    </section>
  );
}
