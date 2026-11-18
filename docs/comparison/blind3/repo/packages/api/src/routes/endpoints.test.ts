import { describe, it, expect, vi, beforeEach } from 'vitest';
import { buildServer } from '../server';

const prismaMock = vi.hoisted(() => ({
  endpoint: { create: vi.fn(), findUnique: vi.fn(), update: vi.fn() },
  delivery: { findMany: vi.fn(), findUnique: vi.fn(), findUniqueOrThrow: vi.fn(), update: vi.fn() },
}));

vi.mock('@tallyhook/shared', async () => {
  const schemas = await vi.importActual<typeof import('@tallyhook/shared/src/schemas')>(
    '@tallyhook/shared/src/schemas',
  );
  return { ...schemas, prisma: prismaMock, logger: false };
});

vi.mock('../queue', () => ({ deliveryQueue: { add: vi.fn() }, redis: {} }));

describe('endpoint routes', () => {
  beforeEach(() => vi.clearAllMocks());

  it('creates an endpoint with a generated secret', async () => {
    prismaMock.endpoint.create.mockImplementation(async ({ data }) => ({ id: 'ep_1', ...data }));
    const app = buildServer();
    const res = await app.inject({
      method: 'POST',
      url: '/v1/endpoints',
      headers: { 'x-tenant-id': 'tnt_1' },
      payload: { url: 'https://example.com/hook' },
    });
    expect(res.statusCode).toBe(201);
    expect(prismaMock.endpoint.create.mock.calls[0][0].data.secret).toHaveLength(48);
  });

  it('does not leak the secret on GET', async () => {
    prismaMock.endpoint.findUnique.mockResolvedValue({ id: 'ep_1', url: 'https://x', secret: 's' });
    const app = buildServer();
    const res = await app.inject({ method: 'GET', url: '/v1/endpoints/ep_1' });
    expect(res.json()).toEqual({ id: 'ep_1', url: 'https://x' });
  });

  it('404s for unknown endpoints', async () => {
    prismaMock.endpoint.findUnique.mockResolvedValue(null);
    const app = buildServer();
    const res = await app.inject({ method: 'GET', url: '/v1/endpoints/nope' });
    expect(res.statusCode).toBe(404);
    expect(res.json()).toEqual({ error: 'not_found' });
  });
});
