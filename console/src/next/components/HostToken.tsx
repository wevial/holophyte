import { useContext, useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { age } from "../../lib/format";
import { addressOf } from "../../lib/hosts";
import { forgetToken, storeToken, tokenFor } from "../../lib/token";
import type { HostCard } from "../hosts";
import { OriginContext } from "../scope";
import { Btn } from "./Btn";
import { useToast } from "./Toast";

export function HostToken({ host }: { host: HostCard }) {
  const origin = useContext(OriginContext);
  const client = useQueryClient();
  const say = useToast();
  const id = useId();
  const [value, setValue] = useState("");
  const [reason, setReason] = useState<string | null>(null);
  const [, rerender] = useState(0);
  const address = addressOf(host.record.base);
  const stored = tokenFor(address);
  const seen = host.record.root_seen_ms ?? host.record.seen_ms;
  const checked = host.record.token_rejected ? "refused: the daemon answered 401"
    : host.down ? "not checked while the host is down"
      : seen != null ? `accepted ${age(host.now - seen)} ago` : "not checked yet";
  const refresh = () => {
    rerender((count) => count + 1);
    void client.invalidateQueries({ queryKey: ["fleet", origin] });
  };
  return (
    <section aria-label={`${host.name} token storage`} className="border-t border-rule p-4 min-[1100px]:p-5">
      <h3 className="mb-3 font-mono text-[11px] uppercase tracking-[0.1em] text-ink-3">Serve token</h3>
      {stored != null ? <div className="flex flex-wrap items-center gap-3">
        <p className="grow font-mono text-[12px] text-ink-3"><span className="mr-3 text-ink">••••</span>saved in this browser · {checked}</p>
        <Btn onClick={() => {
          forgetToken(address);
          refresh();
          say(`Forgot the token for ${host.name} in this browser.`, { label: "Undo", run: () => {
            storeToken(address, stored);
            refresh();
          } });
        }}>Forget</Btn>
      </div> : <form onSubmit={(event) => {
        event.preventDefault();
        if (!value.trim()) return;
        const refused = storeToken(address, value);
        setReason(refused);
        if (refused != null) return;
        setValue("");
        say(`Saved the token for ${host.name} in this browser.`);
        refresh();
      }}>
        {host.record.token_rejected && <p className="mb-3 font-mono text-[12px] text-ink-3">{checked}</p>}
        <p className="mb-3 font-mono text-[12px] text-ink-3">No token in this browser. Actions on {host.name} ask for one.</p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            aria-label={`Serve token for ${host.name}`}
            aria-describedby={reason == null ? undefined : `${id}-reason`}
            type="password"
            autoComplete="off"
            placeholder="Paste the serve token"
            value={value}
            onChange={(event) => setValue(event.currentTarget.value)}
            className="min-h-9 min-w-0 border border-ink-3 bg-paper px-2.5 font-mono text-[13px] text-ink focus:outline-2 focus:outline-act focus:-outline-offset-1"
          />
          <button type="submit" className="btn" data-variant="quiet" disabled={!value.trim()}>Save token</button>
        </div>
        {reason != null && <p id={`${id}-reason`} className="mt-2 font-mono text-[12px] text-bad">{reason}</p>}
      </form>}
    </section>
  );
}
