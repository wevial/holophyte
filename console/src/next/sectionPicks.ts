import { parseBody } from "./ticketForm";

/** Who changed a part from the base: neither or both alike, one side, or each differently. */
export type PartSide = "same" | "theirs" | "mine" | "both";
export type Pick = "mine" | "theirs";

/** A body's preamble ("Title") or one section, keyed by its name and its
 *  occurrence (`Evidence#2`), each side's text null where it lacks it. */
export interface Part {
  key: string;
  name: string;
  base: string | null;
  theirs: string | null;
  mine: string | null;
  side: PartSide;
}

const TITLE = "Title";
const PRIORITY = "Priority";

function partsOf(body: string): Map<string, { name: string; text: string }> {
  const form = parseBody(body);
  const parts = new Map([[TITLE, { name: TITLE, text: form.preamble }]]);
  const seen = new Map([[TITLE, 1], [PRIORITY, 1]]);
  for (const section of form.sections) {
    const count = (seen.get(section.name) ?? 0) + 1;
    seen.set(section.name, count);
    parts.set(count === 1 ? section.name : `${section.name}#${count}`, { name: section.name, text: section.heading + section.text });
  }
  return parts;
}

function sideOf(base: string | null, theirs: string | null, mine: string | null): PartSide {
  if (theirs === mine) return "same";
  if (mine === base) return "theirs";
  if (theirs === base) return "mine";
  return "both";
}

/** Each part of the three bodies, in theirs' order and then mine's and the base's. */
export function sectionParts(base: string, theirs: string, mine: string): Part[] {
  const [was, their, my] = [partsOf(base), partsOf(theirs), partsOf(mine)];
  const keys = new Set([...their.keys(), ...my.keys(), ...was.keys()]);
  return [...keys].map(key => {
    const [b, t, m] = [was.get(key)?.text ?? null, their.get(key)?.text ?? null, my.get(key)?.text ?? null];
    const name = (their.get(key) ?? my.get(key) ?? was.get(key))!.name;
    return { key, name, base: b, theirs: t, mine: m, side: sideOf(b, t, m) };
  });
}

/** The side whose text a part ends up with. */
export const pickedSide = (part: Part, picks: Record<string, Pick>): Pick =>
  part.side === "mine" || (part.side === "both" && picks[part.key] === "mine") ? "mine" : "theirs";

/** Theirs with each part mine changed, or picked as mine, in mine's text;
 *  a picked part theirs lacks follows theirs' sections in mine's order. */
export function pickedBody(theirs: string, mine: string, parts: Part[], picks: Record<string, Pick>): string {
  const byKey = new Map(parts.map(part => [part.key, part]));
  const text = (key: string) => {
    const part = byKey.get(key)!;
    return pickedSide(part, picks) === "mine" ? part.mine : part.theirs;
  };
  const theirKeys = [...partsOf(theirs).keys()];
  const added = [...partsOf(mine).keys()].filter(key => !theirKeys.includes(key));
  const pieces = [...theirKeys, ...added].map(text).filter((piece): piece is string => piece !== null);
  return pieces.reduce((body, piece) => body && !body.endsWith("\n") ? `${body}\n${piece}` : body + piece, "");
}

const priorityText = (priority: number | null) => (priority == null ? "none" : `P${priority}`);

/** Priority, which the body does not hold, as one more part. */
export function priorityPart(base: number | null, theirs: number | null, mine: number | null): Part {
  const [b, t, m] = [priorityText(base), priorityText(theirs), priorityText(mine)];
  return { key: PRIORITY, name: PRIORITY, base: b, theirs: t, mine: m, side: sideOf(b, t, m) };
}
