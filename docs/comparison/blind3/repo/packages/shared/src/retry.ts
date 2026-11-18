export const DEFAULT_MAX_RETRIES = 3;

const BASE_DELAY_MS = 2_000;
const MAX_DELAY_MS = 15 * 60_000;

export function backoffDelay(attempt: number): number {
  const exp = BASE_DELAY_MS * 2 ** Math.max(0, attempt - 1);
  return Math.min(exp, MAX_DELAY_MS);
}

export function shouldRetry(attempt: number, maxRetries = DEFAULT_MAX_RETRIES): boolean {
  return attempt < maxRetries;
}
