const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * Render a duration in milliseconds at two units of precision:
 * `0s`, `45s`, `12m 34s`, `3h 02m`, `3d 4h`.
 */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms));
  if (total >= DAY) {
    const days = Math.floor(total / DAY);
    const hours = Math.floor((total % DAY) / HOUR);
    return `${days}d ${hours}h`;
  }
  if (total >= HOUR) {
    const hours = Math.floor(total / HOUR);
    const minutes = Math.floor((total % HOUR) / MINUTE);
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  }
  if (total >= MINUTE) {
    const minutes = Math.floor(total / MINUTE);
    const seconds = Math.floor((total % MINUTE) / SECOND);
    return `${minutes}m ${seconds}s`;
  }
  return `${Math.floor(total / SECOND)}s`;
}
