import { PrismaClient } from './generated/prisma';

export const prisma = new PrismaClient();

export type { Endpoint, Delivery, DeliveryStatus } from './generated/prisma';
