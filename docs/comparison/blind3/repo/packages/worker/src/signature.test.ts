import { describe, it, expect } from 'vitest';
import { signPayload, verifySignature } from './signature';

describe('signPayload', () => {
  it('is a hex HMAC-SHA256 of the body', () => {
    expect(signPayload('whsec_test', '{"id":1}')).toBe(
      '6b0a3b1e2a8f4d6a0f8c3e1d9b7a5c4e2f0d8b6a4c2e0f9d7b5a3c1e9f7d5b3a',
    );
  });

  it('round-trips through verifySignature', () => {
    const sig = signPayload('whsec_test', 'hello');
    expect(verifySignature('whsec_test', 'hello', sig)).toBe(true);
    expect(verifySignature('whsec_other', 'hello', sig)).toBe(false);
  });
});
