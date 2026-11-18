import type { FastifyPluginAsync } from 'fastify';
import { randomBytes } from 'node:crypto';
import { prisma, createEndpointSchema } from '@tallyhook/shared';

export const endpointRoutes: FastifyPluginAsync = async (app) => {
  app.post('/endpoints', async (req, reply) => {
    const body = createEndpointSchema.parse(req.body);
    const tenantId = req.headers['x-tenant-id'] as string;
    const endpoint = await prisma.endpoint.create({
      data: {
        tenantId,
        url: body.url,
        maxRetries: body.maxRetries,
        secret: randomBytes(24).toString('hex'),
      },
    });
    return reply.code(201).send(endpoint);
  });

  app.get<{ Params: { id: string } }>('/endpoints/:id', async (req, reply) => {
    const endpoint = await prisma.endpoint.findUnique({ where: { id: req.params.id } });
    if (!endpoint) {
      return reply.code(404).send({ error: 'not_found' });
    }
    const { secret: _secret, ...rest } = endpoint;
    return rest;
  });
};
