const SUMMARY_HEADING = /^##\s+Summary\s*$/;

/** The text under the body's `## Summary` heading up to the next heading,
 *  trimmed; empty with no such section. */
export function summaryOf(body: string): string {
  const lines = body.split("\n");
  const start = lines.findIndex((line) => SUMMARY_HEADING.test(line));
  if (start < 0) return "";
  const rest = lines.slice(start + 1);
  const end = rest.findIndex((line) => /^#{1,6}\s/.test(line));
  return (end < 0 ? rest : rest.slice(0, end)).join("\n").trim();
}

/** The first paragraph of the body's `## Summary`, else, with no such
 *  heading, of the body past its headings; empty when it has none. */
export function leadParagraph(body: string): string {
  const hasSummary = body.split("\n").some((line) => SUMMARY_HEADING.test(line));
  const prose = (hasSummary ? summaryOf(body) : body)
    .split("\n")
    .map((line) => (/^#{1,6}\s/.test(line) ? "" : line))
    .join("\n");
  return prose.split(/\n\s*\n/).map((part) => part.trim()).find((part) => part !== "") ?? "";
}
