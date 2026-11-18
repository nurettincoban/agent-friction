import { describe, it, expect } from 'vitest';
import { backoffDelay, shouldRetry } from './retry';

describe('backoffDelay', () => {
  it('doubles from 2s', () => {
    expect(backoffDelay(1)).toBe(2_000);
    expect(backoffDelay(2)).toBe(4_000);
    expect(backoffDelay(3)).toBe(8_000);
  });

  it('caps at 15 minutes', () => {
    expect(backoffDelay(20)).toBe(15 * 60_000);
  });
});

describe('shouldRetry', () => {
  it('stops at maxRetries', () => {
    expect(shouldRetry(2, 3)).toBe(true);
    expect(shouldRetry(3, 3)).toBe(false);
  });
});
