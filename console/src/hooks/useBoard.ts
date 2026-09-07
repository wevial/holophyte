import { useEffect, useRef, useState } from "react";
import { cardsOf, type BoardCard } from "../lib/board";
import type { HostRecord } from "../lib/hosts";
import { defaultPollDeps, fetchJson, type Fetch } from "../lib/poll";
import type { BoardBody } from "../lib/types";

export interface BoardState {
  /** Every host's cards, in host order then the wire's. */
  cards: BoardCard[];
  /** Each host's most recent failure, in host order. */
  errors: string[];
  /** True until every host's first answer, good or bad. */
  loading: boolean;
}

interface Answer {
  body: BoardBody | null;
  error: string | null;
}

/**
 * Each host's `/board`, fetched on mount and again each time `polls`
 * advances (the shell's poll count), so the columns ride the one poll
 * timer. A host's last good body is kept through its failures. The cards
 * are joined afresh each render with the host's current `/status` and
 * `/attention`, so an in-progress card's bar and heartbeat move with the
 * poll even between `/board` answers.
 */
export function useBoard(hosts: HostRecord[], polls = 0, deps: { fetch: Fetch } = defaultPollDeps): BoardState {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const key = hosts.map((host) => host.base).join("\n");

  useEffect(() => {
    let alive = true;
    for (const base of key.split("\n").filter((candidate) => candidate.length > 0)) {
      void (async () => {
        try {
          const body = await fetchJson<BoardBody>(fetchRef.current, `${base}/board`);
          if (alive) setAnswers((all) => ({ ...all, [base]: { body, error: null } }));
        } catch (failure) {
          if (!alive) return;
          const message = failure instanceof Error ? failure.message : String(failure);
          setAnswers((all) => ({ ...all, [base]: { body: all[base]?.body ?? null, error: message } }));
        }
      })();
    }
    return () => {
      alive = false;
    };
  }, [key, polls]);

  const cards = hosts.flatMap((host) => {
    const body = answers[host.base]?.body;
    return body ? cardsOf(host, body) : [];
  });
  return {
    cards,
    errors: hosts.flatMap((host) => (answers[host.base]?.error ? [answers[host.base]!.error!] : [])),
    loading: hosts.some((host) => answers[host.base] == null),
  };
}
