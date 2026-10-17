import { useRef, useState } from "react";
import { ACTIONS_OFF, ROUTES } from "../../lib/actions";
import { useAction } from "../actions";
import type { HostCard } from "../hosts";
import { Btn } from "./Btn";
import { ConfirmDialog } from "./ConfirmDialog";

export function RestartDaemon({ host, onRestart }: {
  host: HostCard;
  onRestart: (key: string, name: string, started: number) => void;
}) {
  const action = useAction();
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const root = host.record.host_status;
  if (!root) return null;
  const unavailable = host.down || host.record.root_failed === true || host.restarting || !root.actions;
  const runs = root.projects.reduce((sum, project) => sum + project.runs.length, 0);
  const keepRunning = () => {
    setOpen(false);
    opener.current?.focus();
  };
  const confirm = async () => {
    if (unavailable || action.isPending) return;
    const started = root.daemon.started_ms;
    const result = await action.mutateAsync({
      label: "Restart daemon", base: host.record.base, route: ROUTES["Restart daemon"]!, body: {},
      said: `Asked ${host.name} to restart its daemon.`,
    });
    if (result.ok) {
      onRestart(host.key, host.name, started);
      setOpen(false);
    }
  };
  return <>
    <Btn ref={opener} variant="danger" disabled={unavailable || action.isPending}
      title={!root.actions ? ACTIONS_OFF : undefined}
      onClick={() => { action.reset(); setOpen(true); }}>Restart daemon</Btn>
    {open && <ConfirmDialog
      ariaLabel={`Restart ${host.name}`}
      label={`Restart · ${host.name}`}
      labelClassName="font-mono"
      heading={`Restart the daemon on ${host.name}?`}
      confirm="Restart daemon"
      confirmVariant="danger-fill"
      cancelLabel="Keep running"
      busyLabel="Restarting…"
      busy={action.isPending}
      confirmDisabled={unavailable}
      onCancel={keepRunning}
      onConfirm={() => void confirm()}
      refusal={action.data && !action.data.ok && <p role="alert" className="mt-3 break-words font-mono text-[12px] text-bad">{action.data.detail}</p>}
    >
      <p className="mt-4 font-read text-[15px] leading-6 text-ink-2">
        The daemon finishes the requests it is answering, stops, and starts again on the checkout&apos;s build {root.build.head?.slice(0, 7) ?? "—"}.
      </p>
      <ul className="mt-4 space-y-3 border-y border-rule py-4 text-[14px] leading-6 text-ink-2">
        <li>{runs === 0 ? "no runs in flight" : runs === 1 ? "1 run in flight keeps going: it runs in the loop, not in this daemon" : `${runs} runs in flight keep going: they run in the loop, not in this daemon`}</li>
        <li>the supervisor and the sweep are not touched</li>
        <li><span aria-hidden="true" className="mr-2 text-ink-3">ⓘ</span>this console loses {host.name} for up to 20s</li>
      </ul>
    </ConfirmDialog>}
  </>;
}
