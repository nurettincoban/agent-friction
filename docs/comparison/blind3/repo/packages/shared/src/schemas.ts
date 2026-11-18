import { z } from 'zod';

export const createEndpointSchema = z.object({
  url: z.string().url(),
  maxRetries: z.number().int().min(0).max(10).optional(),
});

export const listDeliveriesQuery = z.object({
  limit: z.coerce.number().int().min(1).max(100).default(50),
  status: z.enum(['PENDING', 'SUCCEEDED', 'FAILED', 'DEAD']).optional(),
});

export type CreateEndpointInput = z.infer<typeof createEndpointSchema>;
export type ListDeliveriesQuery = z.infer<typeof listDeliveriesQuery>;
