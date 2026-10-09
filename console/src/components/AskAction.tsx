import { useState } from "react";
import { useRunAsks } from "../hooks/useRunAsks";
import { ROUTES, postAction } from "../lib/actions";
import type { Fetch } from "../lib/poll";
import { prLabel } from "../lib/shipped";
import { ActionButton } from "./ActionButton";
import { Markdown } from "./Markdown";

const pageFetch: Fetch = (url, init) => globalThis.fetch(url, init);

/** A parked pull request's Ask, drawn from the latest of the daemon's asks
 *  for `runId`: an unanswered one shows its question as waiting and no Ask
 *  (the daemon refuses a second as `ask_pending`); an answered one links its
 *  comment and shows the answer beside Ask. Ask opens an in-row question box
 *  whose Send posts `/actions/ask` once and shows the daemon's answer. */
export function AskAction({ base, runId, prUrl, polls, deps, fetch }: {
  base: string;
  runId: number;
  prUrl?: string | null;
  polls: number;
  deps: { fetch: Fetch };
  fetch?: Fetch;
}) {
  const asks = useRunAsks(base, runId, polls, deps);
  const [asking, setAsking] = useState(false);
  const [question, setQuestion] = useState("");
  const [posting, setPosting] = useState(false);
  // The sent question and the ask ids listed when it was sent: it shows as
  // waiting until a read lists an ask beyond them.
  const [sent, setSent] = useState<{ question: string; known: Set<number> } | null>(null);
  const [answer, setAnswer] = useState<{ text: string; ok: boolean } | null>(null);
  if (asks == null) return null;
  const send = async () => {
    const text = question.trim();
    setPosting(true);
    try {
      const result = await postAction(base, ROUTES.Ask!, { run: runId, question: text }, fetch ?? pageFetch);
      setAnswer({ text: result.detail, ok: result.ok });
      if (result.ok) {
        setSent({ question: text, known: new Set(asks.asks.map(ask => ask.id)) });
        setAsking(false);
        setQuestion("");
      }
    } finally {
      setPosting(false);
    }
  };
  const latest = asks.asks.at(-1);
  const unlisted = sent != null && asks.asks.every(ask => sent.known.has(ask.id));
  const waiting = unlisted ? sent.question : latest && latest.answer == null ? latest.question : null;
  const answered = waiting == null && latest?.answer != null ? { url: latest.url, text: latest.answer } : null;
  const pr = prUrl ?? asks.pr_url;
  return (
    <div className="flex flex-col items-end gap-1.5">
      {waiting != null ? (
        <div data-ask-waiting className="max-w-[280px] text-right text-[12px]">
          <p className="text-ink">Asked: {waiting}</p>
          <p className="text-muted">waiting for the answer</p>
        </div>
      ) : answered && (
        <div data-ask-answer className="flex max-w-[280px] flex-col items-end gap-1 text-[12px]">
          <a className="text-link" href={answered.url ?? pr ?? undefined} target="_blank" rel="noopener noreferrer">
            Answer on {pr ? prLabel(pr) : "the pull request"}
          </a>
          <div className="text-left text-body"><Markdown>{answered.text}</Markdown></div>
        </div>
      )}
      {asking ? (
        <div role="group" aria-label="Ask about the pull request" className="flex flex-col items-end gap-1.5">
          <textarea aria-label="Question about the pull request" value={question} disabled={posting}
            onChange={event => setQuestion(event.target.value)}
            className="w-[280px] rounded border border-line bg-card p-2 text-[12px] text-ink" />
          <div className="flex gap-1.5">
            <ActionButton onAct={send} disabled={posting || !question.trim()}
              title={posting ? "Sending the question" : "Write a question to send"}>Send</ActionButton>
            <button type="button" disabled={posting} onClick={() => { setAsking(false); setQuestion(""); }}
              className="rounded-button px-2 py-1 text-[12px] text-muted disabled:opacity-60">Cancel</button>
          </div>
        </div>
      ) : waiting == null && <ActionButton onAct={async () => { setAnswer(null); setAsking(true); }}>Ask</ActionButton>}
      {answer && (
        <p data-action-detail data-ok={answer.ok} role="status"
          className={`max-w-[280px] text-right text-[12px] ${answer.ok ? "text-muted" : "text-needs-you-link"}`}>
          {answer.text}
        </p>
      )}
    </div>
  );
}
