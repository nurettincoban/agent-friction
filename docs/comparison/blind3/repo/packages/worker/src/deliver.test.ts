import { describe, it, expect, vi, afterEach } from 'vitest';

vi.mock('@tallyhook/shared', () => ({
  prisma: {},
  logger: { warn: vi.fn(), info: vi.fn(), error: vi.fn() },
  backoffDelay: () => 1000,
  shouldRetry: () => true,
}));

import { postWebhook } from './deliver';

describe('postWebhook', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('sends the signature header and JSON body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);

    await postWebhook('https://example.com/hook', '{"a":1}', 'abc123');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://example.com/hook');
    expect(init.headers['x-tallyhook-signature']).toBe('abc123');
    expect(init.body).toBe('{"a":1}');
  });
});
