import type { FastifyPluginAsync } from 'fastify';
import { prisma, listDeliveriesQuery } from '@tallyhook/shared';

export const deliveryRoutes: FastifyPluginAsync = async (app) => {
  app.get<{ Params: { id: string } }>('/endpoints/:id/deliveries', async (req) => {
    const q = listDeliveriesQuery.parse(req.query);
    const items = await prisma.delivery.findMany({
      where: { endpointId: req.params.id, status: q.status },
      orderBy: { createdAt: 'desc' },
      take: q.limit,
    });
    return { items };
  });
};
