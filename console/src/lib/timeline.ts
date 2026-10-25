/** The round a phase change names (`round 3 review`, `round 3: …`), or null. */
export function roundNumber(summary: string): number | null {
  const match = /\bround\s+(\d+)/i.exec(summary);
  return match ? Number(match[1]) : null;
}
