export interface QuietWindow {
  /** hour of day (0-23) the window opens */
  start: number;
  /** hour of day (0-23) the window closes */
  end: number;
}

export const DEFAULT_QUIET_WINDOW: QuietWindow = { start: 22, end: 7 };

export function isQuietHours(window: QuietWindow, at: Date = new Date()): boolean {
  const h = at.getHours();
  if (window.start <= window.end) {
    return h >= window.start && h < window.end;
  }
  return h >= window.start || h < window.end;
}
