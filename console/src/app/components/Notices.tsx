import type { SilentHost, StaleConsole } from "../notices";
import { Btn } from "./Btn";

function InfoIcon() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true" className="size-4 shrink-0 text-wait">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 4.8v3.6M8 11.1v.1" />
    </svg>
  );
}

export function WarnIcon() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-need">
      <path d="M8 2 14.5 13.5h-13z" />
      <path d="M8 6.3v3.2M8 11.6v.1" />
    </svg>
  );
}

const detailClass = "mt-0.5 font-mono text-[12px] leading-[1.5]";

export function FirstLoad({ line }: { line: string }) {
  return (
    <div role="status" className="flex items-center gap-3 font-mono text-[13px] leading-[1.4] text-ink-2">
      <span className="think" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      {line}
    </div>
  );
}

/** The strip above the Overview's sections: the stale console, each host
 *  not answering, then one line per outage or migration. */
export function Notices({ stale, silent, lines }: { stale: StaleConsole | null; silent: SilentHost[]; lines: string[] }) {
  if (stale == null && silent.length === 0 && lines.length === 0) return null;
  return (
    <div aria-label="Notices" className="flex flex-col gap-2.5">
      {stale && (
        <div role="status" data-notice="stale-console" className="flex flex-wrap items-center gap-x-4 gap-y-3 border border-rule bg-paper-2 px-3.5 py-3 text-[14px] text-ink">
          <InfoIcon />
          <div className="min-w-0 flex-[1_1_260px]">
            <div className="font-medium">{stale.title}</div>
            <div className={`${detailClass} text-ink-3`}>{stale.builds}</div>
            {stale.reason != null && <div className={`${detailClass} text-ink-3`}>{stale.reason}</div>}
          </div>
          <Btn variant="primary" onClick={() => window.location.reload()}>
            Reload
          </Btn>
        </div>
      )}
      {silent.map((host) => (
        <div key={host.title} role="status" data-notice="host-silent" className="flex items-start gap-3 bg-need-wash px-3.5 py-3 text-[14px] text-ink shadow-[inset_2px_0_0_var(--need)]">
          <WarnIcon />
          <div className="min-w-0">
            <div className="font-medium">{host.title}</div>
            <div className={`${detailClass} text-ink-2`}>{host.detail}</div>
          </div>
        </div>
      ))}
      {lines.length > 0 && (
        <ul className="flex flex-col gap-1 font-mono text-[12.5px] leading-[1.5] text-ink-2">
          {lines.map((line, index) => (
            <li key={`${index}:${line}`}>{line}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
