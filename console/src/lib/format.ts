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

/**
 * A duration for a sentence, seconds padded like the minutes above:
 * `7m 01s`, `3h 02m`, `3d 4h`; under a minute plain `45s`.
 */
export function formatSpan(ms: number): string {
  const total = Math.max(0, Math.floor(ms));
  if (total >= HOUR) return formatDuration(total);
  if (total >= MINUTE) {
    const minutes = Math.floor(total / MINUTE);
    const seconds = Math.floor((total % MINUTE) / SECOND);
    return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  }
  return `${Math.floor(total / SECOND)}s`;
}

/** An age at a glance, one unit, rounded down: `45s`, `7m`, `2h`, `3d`. */
export function formatAge(ms: number): string {
  const total = Math.max(0, Math.floor(ms));
  if (total >= DAY) return `${Math.floor(total / DAY)}d`;
  if (total >= HOUR) return `${Math.floor(total / HOUR)}h`;
  if (total >= MINUTE) return `${Math.floor(total / MINUTE)}m`;
  return `${Math.floor(total / SECOND)}s`;
}

/** Wall-clock time of an epoch-ms instant in the viewer's zone: `14:07`. */
export function formatClock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}
