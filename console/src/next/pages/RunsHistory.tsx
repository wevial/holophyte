import { useContext } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { formatSettled } from "../../lib/format";
import { Btn } from "../components/Btn";
import { ChevronIcon } from "../components/icons";
import { useHistoryDays, useHistoryTotal } from "../queries";
import { dayGroups, PAGE_COUNT, PAGE_DAYS, pastDays } from "../runDays";
import { PollDepsContext, scopedHosts, useConsolePeers } from "../scope";

export function validateHistorySearch({ page }: Record<string, unknown>): { page?: number } {
  const number = Number(page);
  return { page: Number.isInteger(number) && number > 1 && number <= PAGE_COUNT ? number : undefined };
}

const dayName = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
const rangeName = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

function DayHead({ day }: { day: ReturnType<typeof dayGroups>[number] }) {
  return (
    <header className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-rule px-4 pt-3.5 pb-3">
      <h2 id={`day-${day.since}`} className="font-sans text-[16px] font-semibold text-ink">{dayName.format(day.since)}</h2>
      <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[12.5px] text-ink-3">
        <span><b className="font-semibold text-ink">{day.counts.runs}</b> runs</span>
        <span><b className="font-semibold text-live">{day.counts.merged}</b> merged</span>
        <span><b className={`font-semibold ${day.counts.failed > 0 ? "text-bad" : "text-ink-2"}`}>{day.counts.failed}</b> failed</span>
        {day.counts.abandoned > 0 && <span><b className="font-semibold">{day.counts.abandoned}</b> abandoned</span>}
      </div>
      {day.workMs != null && day.estimateMs != null && (
        <p className="ml-auto font-mono text-[12px] text-ink-3">
          {formatSettled(day.workMs)} of work against {formatSettled(day.estimateMs)} estimated
        </p>
      )}
    </header>
  );
}

export function RunsHistory() {
  const { project, page: pickedPage } = useSearch({ from: "/runs/history" });
  const page = pickedPage ?? 1;
  const navigate = useNavigate();
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const hosts = scopedHosts(peers.hosts, project);
  const days = pastDays(peers.now).slice((page - 1) * PAGE_DAYS, page * PAGE_DAYS);
  const { answers } = useHistoryDays(hosts, days, deps);
  const { total } = useHistoryTotal(hosts, peers.now, deps);
  const groups = dayGroups(hosts, answers, days);
  const pick = (next: number) => void navigate({ to: "/runs/history", search: { project, page: next === 1 ? undefined : next } });
  return (
    <section aria-labelledby="page-title" className="mx-auto flex max-w-[1320px] flex-col gap-[18px] px-8 py-7 max-[720px]:px-2">
      <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Runs history</h1>
        <Link to="/runs" search={{ project }} className="self-start font-mono text-[12.5px] text-act underline underline-offset-2">Today and yesterday</Link>
        <p className="font-mono text-[13px] leading-[1.6] text-ink-2 [overflow-wrap:anywhere]">
          {project ?? "all projects"} · {total != null ? `${total} runs in the` : "the"} 30 days before yesterday · newest day first
        </p>
      </header>
      <nav aria-label="Pages of days" className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-x-3 gap-y-2">
        <Btn disabled={page === 1} onClick={() => pick(page - 1)}><ChevronIcon folded={false} /> Newer days</Btn>
        <span className="font-mono text-[12.5px] text-ink-2">{rangeName.format(days[0]!.since)} to {rangeName.format(days[PAGE_DAYS - 1]!.since)} · page {page} of {PAGE_COUNT}</span>
        <Btn disabled={page === PAGE_COUNT} onClick={() => pick(page + 1)}>Older days <ChevronIcon folded /></Btn>
      </nav>
      {groups.map(day => (
        <section key={day.since} aria-labelledby={`day-${day.since}`} className="border border-rule bg-paper">
          <DayHead day={day} />
          {day.rows.length === 0 && <p className="px-4 py-4 font-mono text-[12.5px] text-ink-3 [overflow-wrap:anywhere]">{project ? `No runs in ${project} this day.` : "No runs this day."}</p>}
        </section>
      ))}
    </section>
  );
}
