import { groupByDay, medianRounds } from "../lib/shipped";
import type { ShippedState } from "../hooks/useShipped";
import { ShippedTable } from "./ShippedTable";

export { SHIPPED_PAGE, concatLedgers, shippedUrl } from "../hooks/useShipped";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/**
 * The Shipped view: the merge ledger the shell holds (`useShipped`, one
 * ledger shared with the Board's Shipped-today table), every daemon's
 * rows newest first under a sub-header per day; "Load older" fetches, for
 * every daemon with more, the page before the oldest id it has shown.
 * `now` is the clock naming "Today"; `tz` pins the zone for tests.
 */
export function Shipped({ shipped, now, tz }: { shipped: ShippedState; now: number; tz?: string }) {
  const { rows, more, errors, loading, paging, loadOlder } = shipped;
  const days = groupByDay(rows, now, tz).length;
  const median = medianRounds(rows);
  const subtitle =
    rows.length === 0
      ? null
      : `${plural(rows.length, "merge")} · last ${plural(days, "day")} · median ${median} rounds${more ? ` (of ${rows.length} loaded)` : ""}`;

  return (
    <section aria-label="Shipped" className="px-6 pt-6 pb-6">
      <div className="flex items-baseline gap-3">
        <h1 className="text-[20px] font-semibold text-ink">Shipped</h1>
        {subtitle && (
          <span data-subtitle className="text-[13px] text-muted">
            {subtitle}
          </span>
        )}
      </div>
      {errors.map((error) => (
        <p key={error} role="alert" className="mt-2 font-mono text-[11px] text-bad-text">
          shipped failed: {error}
        </p>
      ))}
      {!loading && rows.length === 0 ? (
        <p className="mt-3 text-[13px] text-muted">Nothing merged yet</p>
      ) : (
        <div className="mt-3">
          <ShippedTable rows={rows} now={now} tz={tz} />
          {more && (
            <button
              type="button"
              disabled={paging}
              onClick={() => void loadOlder()}
              className="mt-3 rounded-button border border-line bg-card px-3 py-1.5 text-[13px] font-semibold text-body hover:bg-hover disabled:opacity-60"
            >
              Load older
            </button>
          )}
        </div>
      )}
    </section>
  );
}
