import { describe, it, expect } from 'vitest';
import { isQuietHours, DEFAULT_QUIET_WINDOW } from './quietHours';

describe('isQuietHours', () => {
  it('treats 23:30 as quiet for the default window', () => {
    expect(isQuietHours(DEFAULT_QUIET_WINDOW, new Date('2026-01-01T23:30:00'))).toBe(true);
  });

  it('handles windows that do not wrap midnight', () => {
    const lunch = { start: 12, end: 13 };
    expect(isQuietHours(lunch, new Date('2026-01-01T12:15:00'))).toBe(true);
    expect(isQuietHours(lunch, new Date('2026-01-01T13:00:00'))).toBe(false);
  });

  it('does not hold deliveries for a freshly created endpoint', () => {
    // a brand-new endpoint with the default window should deliver right away
    expect(isQuietHours(DEFAULT_QUIET_WINDOW)).toBe(false);
  });

  it('treats 06:59 as still quiet', () => {
    expect(isQuietHours(DEFAULT_QUIET_WINDOW, new Date('2026-01-01T06:59:00'))).toBe(true);
  });
});
