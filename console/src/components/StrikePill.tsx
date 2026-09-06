import { strikeTone } from "../lib/runs";

/** `strike S/T` on the warn wash below the last strike, the bad one at it;
 *  nothing with no strikes. */
export function StrikePill({ strikes, max }: { strikes: number; max: number }) {
  const tone = strikeTone(strikes, max);
  if (tone == null) return null;
  const wash = tone === "red" ? "bg-bad-bg text-bad-text" : "bg-warn-bg text-warn-text";
  return (
    <span
      data-strike={tone}
      className={`inline-block shrink-0 rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${wash}`}
    >
      strike {strikes}/{max}
    </span>
  );
}
