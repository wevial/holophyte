type TitleSpan = { text: string; code: boolean };

/** Splits a title on paired backticks; an unpaired backtick stays text. */
function splitTitle(title: string): TitleSpan[] {
  const spans: TitleSpan[] = [];
  let rest = title;
  for (;;) {
    const open = rest.indexOf("`");
    const close = open < 0 ? -1 : rest.indexOf("`", open + 1);
    if (close < 0) break;
    if (open > 0) spans.push({ text: rest.slice(0, open), code: false });
    if (close > open + 1) spans.push({ text: rest.slice(open + 1, close), code: true });
    rest = rest.slice(close + 1);
  }
  if (rest !== "") spans.push({ text: rest, code: false });
  return spans;
}

/** The title as an accessible name: paired backticks dropped. */
export function plainTitle(title: string): string {
  return splitTitle(title)
    .map((span) => span.text)
    .join("");
}

export function TitleText({ children }: { children: string }) {
  return (
    <>
      {splitTitle(children).map((span, index) =>
        span.code ? (
          <code key={index} className="rounded-[3px] bg-paper-2 px-1 font-mono">
            {span.text}
          </code>
        ) : (
          span.text
        ),
      )}
    </>
  );
}
