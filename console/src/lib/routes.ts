/** A route label split for display: "claude-implement opus" is Claude,
 *  Opus. */
export interface RouteParts {
  harness: string;
  model: string;
}

const HARNESSES = new Map([
  ["claude", "Claude"],
  ["codex", "Codex"],
  ["devin", "Devin"],
  ["cursor", "Cursor"],
]);

/** A model word title-cased part by part, `gpt` as GPT, a part that is a
 *  version number kept hyphenated to the one before it: "gpt-6-astra" is
 *  "GPT-6 Astra". */
function modelWord(word: string): string {
  return word
    .split("-")
    .filter((part) => part !== "")
    .map((part) => (part === "gpt" ? "GPT" : part[0]!.toUpperCase() + part.slice(1)))
    .reduce((out, part) => (out === "" ? part : /^\d/.test(part) ? `${out}-${part}` : `${out} ${part}`), "");
}

/** A daemon's route label (`route_labels()` or `turn_label()`: the
 *  command's first word, then its model if it names one) as a harness and
 *  a model. The harness drops a trailing `-implement`, `-review` or
 *  `-adjudicate` and reads as its display name, an unknown one as itself;
 *  the model is empty when the label names none. */
export function routeParts(label: string): RouteParts {
  const [command = "", ...rest] = label.trim().split(/\s+/);
  const word = command.replace(/-(implement|review|adjudicate)$/, "");
  return { harness: HARNESSES.get(word) ?? word, model: rest.map(modelWord).join(" ") };
}

/** "Claude · Opus", or the harness alone when the label names no model. */
export function routeText(label: string): string {
  const { harness, model } = routeParts(label);
  return model ? `${harness} · ${model}` : harness;
}
