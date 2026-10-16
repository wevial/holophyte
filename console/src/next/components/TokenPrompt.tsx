import { useContext, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { addressOf } from "../../lib/hosts";
import { storeToken, tokenFor } from "../../lib/token";
import { OriginContext, useConsolePeers } from "../scope";

/** The request addresses whose daemon answered 401 and holds no token. */
function wantingAddresses(bases: string[]): string[] {
  return [...new Set(bases.map(addressOf))].filter((address) => tokenFor(address) == null);
}

function TokenForm({ address, onStored }: { address: string; onStored: () => void }) {
  const [token, setToken] = useState("");
  const [reason, setReason] = useState<string | null>(null);
  const id = `token-${address}`;
  return (
    <form
      data-token-form
      className="flex flex-wrap items-end gap-2 border border-rule bg-paper-2 px-3.5 py-3"
      onSubmit={(event) => {
        event.preventDefault();
        const refused = storeToken(address, token);
        setReason(refused);
        if (refused == null) onStored();
      }}
    >
      <label className="flex min-w-0 flex-[1_1_260px] flex-col gap-1">
        <span className="text-[14px] font-medium text-ink">{address} asks for its token.</span>
        <input
          id={id}
          name="token"
          type="password"
          autoComplete="off"
          aria-describedby={reason == null ? undefined : `${id}-reason`}
          value={token}
          onChange={(event) => setToken(event.currentTarget.value)}
          className="min-h-9 border border-ink-3 bg-paper px-2.5 font-mono text-[13px] text-ink focus:outline-2 focus:outline-act focus:-outline-offset-1"
        />
        {reason != null && (
          <span id={`${id}-reason`} className="font-mono text-[12px] text-bad">
            {reason}
          </span>
        )}
      </label>
      <button type="submit" className="btn" data-variant="primary">
        Use
      </button>
    </form>
  );
}

/** A token field per daemon that answered 401 with none stored. A kept
 *  token is sent by the poll it starts; a 401 to it forgets it, and the
 *  field is back. */
export function TokenPrompt() {
  const { hosts } = useConsolePeers();
  const origin = useContext(OriginContext);
  const client = useQueryClient();
  const [, rerender] = useState(0);
  const addresses = wantingAddresses(hosts.filter((host) => host.needs_token).map((host) => host.base));
  if (addresses.length === 0) return null;
  return (
    <div aria-label="Tokens" className="flex flex-col gap-2.5 px-8 pt-7">
      {addresses.map((address) => (
        <TokenForm
          key={address}
          address={address}
          onStored={() => {
            rerender((count) => count + 1);
            void client.invalidateQueries({ queryKey: ["fleet", origin] });
          }}
        />
      ))}
    </div>
  );
}
