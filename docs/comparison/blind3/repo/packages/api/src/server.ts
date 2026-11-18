import Fastify from 'fastify';
import { logger } from '@tallyhook/shared';
import { endpointRoutes } from './routes/endpoints';
import { deliveryRoutes } from './routes/deliveries';
import { deadLetterRoutes } from './routes/deadLetters';

export function buildServer() {
  const app = Fastify({ loggerInstance: logger });

  app.get('/healthz', async () => ({ ok: true }));

  app.register(endpointRoutes, { prefix: '/v1' });
  app.register(deliveryRoutes, { prefix: '/v1' });
  app.register(deadLetterRoutes, { prefix: '/v1' });

  return app;
}

if (require.main === module) {
  const port = Number(process.env.PORT ?? 3000);
  buildServer().listen({ port, host: '0.0.0.0' });
}
