import { describe, it, expect, afterAll } from 'vitest';
import { prisma } from '@tallyhook/shared';
import { buildServer } from '../src/server';
import { redis, deliveryQueue } from '../src/queue';

// Runs against the real Postgres + Redis from docker-compose.yml.
describe('POST /v1/dead-letters/:id/requeue', () => {
  afterAll(async () => {
    await deliveryQueue.close();
    await redis.quit();
    await prisma.$disconnect();
  });

  it('moves a dead delivery back to PENDING and enqueues it', async () => {
    const endpoint = await prisma.endpoint.create({
      data: { tenantId: 'tnt_int', url: 'http://localhost:9/hook', secret: 'x' },
    });
    const delivery = await prisma.delivery.create({
      data: { endpointId: endpoint.id, eventType: 'test', payload: {}, status: 'DEAD', attempts: 3 },
    });

    const app = buildServer();
    const res = await app.inject({ method: 'POST', url: `/v1/dead-letters/${delivery.id}/requeue` });

    expect(res.statusCode).toBe(202);
    const after = await prisma.delivery.findUniqueOrThrow({ where: { id: delivery.id } });
    expect(after.status).toBe('PENDING');
    expect(await deliveryQueue.getWaitingCount()).toBeGreaterThan(0);
  });
});
