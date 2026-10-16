import { RunTable } from "../components/RunTable";
import { useRunHistory } from "../history";

export function Runs() {
  const { now, rows } = useRunHistory();
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-8 py-7 max-[720px]:px-2">
      <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Runs</h1>
      <RunTable rows={rows} now={now} />
    </section>
  );
}
