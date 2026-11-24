import { useContext, useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { age } from "../../lib/format";
import { addressOf } from "../../lib/hosts";
import { forgetToken, storeToken, tokenEpoch, tokenFor } from "../../lib/token";
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
  const form = useRef<HTMLFormElement>(null);
  const action = useRef<HTMLButtonElement>(null);
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
  const forget = () => {
    if (stored == null) return;
    forgetToken(address);
    refresh();
    const gen = tokenEpoch(address);
    let stale = false;
    const valid = () => !(stale ||= tokenEpoch(address) !== gen || tokenFor(address) != null);
    say(`Forgot the token for ${host.name} in this browser.`, {
      label: "Undo",
      valid,
      run: () => {
        if (!valid()) return;
        storeToken(address, stored);
        refresh();
      },
    });
  };
  return (
    <section aria-label={`${host.name} token storage`} className="border-t border-hair p-4 min-[1100px]:p-5">
      <h3 className="mb-2 font-sans text-[13px] font-semibold text-ink">Serve token</h3>
      <form
        ref={form}
        onSubmit={(event) => {
          event.preventDefault();
          if (stored != null || !value.trim()) return;
          const refused = storeToken(address, value);
          setReason(refused);
          if (refused != null) return;
          if (form.current?.contains(document.activeElement) && document.activeElement !== action.current) {
            action.current?.focus();
          }
          setValue("");
          say(`Saved the token for ${host.name} in this browser.`);
          refresh();
        }}
      >
        {stored == null && host.record.token_rejected && <p className="mb-2 font-sans text-[13px] text-ink-3">{checked}</p>}
        {stored == null && <p className="mb-3 font-sans text-[13px] text-ink-3">No token in this browser. Actions on {host.name} ask for one.</p>}
        <div className={`flex flex-wrap items-center ${stored != null ? "gap-3" : "gap-2"}`}>
          {stored != null ? (
            <p className="grow font-sans text-[13px] text-ink-3"><span className="mr-3 font-mono text-ink">••••</span>saved in this browser · {checked}</p>
          ) : (
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
          )}
          <Btn
            ref={action}
            className={stored != null ? undefined : "!h-7 !rounded-[7px] !px-2.5 !text-[12.5px]"}
            disabled={stored == null && !value.trim()}
            onClick={stored != null ? forget : () => form.current?.requestSubmit()}
          >
            {stored != null ? "Forget" : "Save token"}
          </Btn>
        </div>
        {stored == null && reason != null && <p id={`${id}-reason`} className="mt-2 font-sans text-[13px] text-bad">{reason}</p>}
      </form>
    </section>
  );
}
