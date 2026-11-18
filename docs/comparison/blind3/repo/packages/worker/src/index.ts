import { Worker } from 'bullmq';
import IORedis from 'ioredis';
import { logger } from '@tallyhook/shared';
import { processDelivery } from './deliver';

const connection = new IORedis(process.env.REDIS_URL ?? 'redis://localhost:6379', {
  maxRetriesPerRequest: null,
});

const worker = new Worker(
  'deliveries',
  async (job) => processDelivery(job.data.deliveryId),
  { connection, concurrency: Number(process.env.WORKER_CONCURRENCY ?? 10) },
);

worker.on('failed', (job, err) => {
  logger.error({ jobId: job?.id, err }, 'delivery job failed');
});
