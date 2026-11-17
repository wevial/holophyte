import { useContext, useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { age } from "../../lib/format";
import { addressOf } from "../../lib/hosts";
import { forgetToken, storeToken, tokenFor } from "../../lib/token";
import type { HostCard } from "../hosts";
import { OriginContext } from "../scope";
import { Btn } from "./Btn";
import { useToast } from "./Toast";

const SMALL = "!h-7 !rounded-[7px] !px-2.5 !text-[12.5px]";

export function HostToken({ host }: { host: HostCard }) {
  const origin = useContext(OriginContext);
  const client = useQueryClient();
  const say = useToast();
  const id = useId();
  const [value, setValue] = useState("");
  const [reason, setReason] = useState<string | null>(null);
  const [, rerender] = useState(0);
  const form = useRef<HTMLFormElement>(null);
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
    <section aria-label={`${host.name} token storage`} className="border-t border-hair p-4 min-[1100px]:p-5">
      <h3 className="mb-2 font-sans text-[13px] font-semibold text-ink">Serve token</h3>
      {stored != null ? <div className="flex flex-wrap items-center gap-3">
        <p className="grow font-sans text-[13px] text-ink-3"><span className="mr-3 font-mono text-ink">••••</span>saved in this browser · {checked}</p>
        <Btn className={SMALL} onClick={() => {
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
      }} ref={form}>
        {host.record.token_rejected && <p className="mb-2 font-sans text-[13px] text-ink-3">{checked}</p>}
        <p className="mb-3 font-sans text-[13px] text-ink-3">No token in this browser. Actions on {host.name} ask for one.</p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            aria-label={`Serve token for ${host.name}`}
            aria-describedby={reason == null ? undefined : `${id}-reason`}
            type="password"
            autoComplete="off"
            placeholder="Paste the serve token"
            value={value}
            onChange={(event) => setValue(event.currentTarget.value)}
            className="field w-72 min-w-0 max-w-full"
          />
          <Btn className={SMALL} disabled={!value.trim()} onClick={() => form.current?.requestSubmit()}>Save token</Btn>
        </div>
        {reason != null && <p id={`${id}-reason`} className="mt-2 font-sans text-[13px] text-bad">{reason}</p>}
      </form>}
    </section>
  );
}
