import { prisma } from '../src/db';

async function main() {
  const endpoint = await prisma.endpoint.create({
    data: {
      tenantId: 'tnt_demo',
      url: 'http://localhost:4000/hooks',
      secret: 'whsec_demo_only',
    },
  });
  await prisma.delivery.create({
    data: { endpointId: endpoint.id, eventType: 'invoice.paid', payload: { id: 'inv_1' } },
  });
}

main().finally(() => prisma.$disconnect());
