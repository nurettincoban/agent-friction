import type { FastifyPluginAsync } from 'fastify';
import { prisma } from '@tallyhook/shared';
import { deliveryQueue } from '../queue';

export const deadLetterRoutes: FastifyPluginAsync = async (app) => {
  app.post<{ Params: { id: string } }>('/dead-letters/:id/requeue', async (req, reply) => {
    const delivery = await prisma.delivery.findUniqueOrThrow({ where: { id: req.params.id } });
    if (delivery.status !== 'DEAD') {
      return reply.code(409).send({ error: 'not_dead' });
    }
    await prisma.delivery.update({
      where: { id: delivery.id },
      data: { status: 'PENDING', attempts: 0, nextAttemptAt: new Date() },
    });
    await deliveryQueue.add('deliver', { deliveryId: delivery.id });
    return reply.code(202).send({ requeued: true });
  });
};
