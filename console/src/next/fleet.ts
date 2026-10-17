import { QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { pollPeers, type PeersState } from "../hooks/usePeers";
import { useTick } from "../hooks/useTick";
import {
  attentionSchema,
  hostAttentionSchema,
  hostStatusSchema,
  statusSchema,
  type Attention,
  type HostAttention,
  type HostStatus,
  type Status,
} from "../lib/contract.gen";
import { addressOf, mergeHosts, oldestPoll, type HostRecord } from "../lib/hosts";
import {
  POLL_INTERVAL_MS,
  REQUEST_TIMEOUT_MS,
  TICK_MS,
  type PollDeps,
  type PollSchemas,
} from "../lib/poll";
import { forgetToken } from "../lib/token";

/** The daemon's bodies as the generated contract types them. */
export interface ContractBodies {
  status: Status;
  attention: Attention;
  host: HostStatus;
  hostAttention: HostAttention;
}

const contractSchemas: PollSchemas<ContractBodies> = {
  status: statusSchema,
  attention: attentionSchema,
  host: hostStatusSchema,
  hostAttention: hostAttentionSchema,
};

interface Fleet {
  hosts: HostRecord<ContractBodies>[];
  polls: number;
}

/** The new console's one cache. A failed read is not retried: the next
 *  poll is the retry, and the failure shows at once. */
export function consoleQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/** Every daemon `/peers` names, as `usePeers()` polls it, but as one query
 *  per origin: every view that reads it shares one poll every
 *  `POLL_INTERVAL_MS`, and each body is parsed with the generated contract. */
export function useFleet(origin: string, deps: PollDeps): PeersState<ContractBodies> {
  const client = useQueryClient();
  const depsRef = useRef(deps);
  depsRef.current = deps;
  const queryKey = ["fleet", origin];
  const { data } = useQuery({
    queryKey,
    queryFn: async (): Promise<Fleet> => {
      const previous = client.getQueryData<Fleet>(queryKey);
      const known = previous?.hosts ?? [];
      const results = await pollPeers(origin, known, depsRef.current, REQUEST_TIMEOUT_MS, contractSchemas);
      for (const result of results) if (!result.ok && result.status === 401 && result.token_sent) forgetToken(addressOf(result.base));
      return { hosts: mergeHosts(known, results, depsRef.current.now()), polls: (previous?.polls ?? 0) + 1 };
    },
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
  useTick(TICK_MS);
  const now = deps.now();
  const hosts = data?.hosts ?? [];
  const oldest = oldestPoll(hosts);
  return {
    hosts,
    polls: data?.polls ?? 0,
    now,
    polledAgo: oldest == null ? null : Math.max(0, now - oldest),
  };
}
