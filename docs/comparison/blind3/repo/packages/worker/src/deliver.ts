import { prisma, logger, backoffDelay, shouldRetry } from '@tallyhook/shared';
import { signPayload } from './signature';
import { isQuietHours, DEFAULT_QUIET_WINDOW } from './quietHours';

const REQUEST_TIMEOUT_MS = 10_000;

export async function postWebhook(url: string, body: string, signature: string): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-tallyhook-signature': signature,
    },
    body,
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
}

export async function processDelivery(deliveryId: string): Promise<void> {
  const delivery = await prisma.delivery.findUniqueOrThrow({
    where: { id: deliveryId },
    include: { endpoint: true },
  });
  const { endpoint } = delivery;
  if (endpoint.disabledAt) {
    return;
  }
  if (isQuietHours(DEFAULT_QUIET_WINDOW)) {
    // hold until the window closes; the scheduler picks it up again
    return;
  }

  const body = JSON.stringify(delivery.payload);
  const signature = signPayload(endpoint.secret, body);
  const attempt = delivery.attempts + 1;

  try {
    const res = await postWebhook(endpoint.url, body, signature);
    if (!res.ok) {
      throw new Error(`endpoint responded ${res.status}`);
    }
    await prisma.delivery.update({
      where: { id: delivery.id },
      data: { status: 'SUCCEEDED', attempts: attempt, lastError: null },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (shouldRetry(attempt, endpoint.maxRetries)) {
      await prisma.delivery.update({
        where: { id: delivery.id },
        data: {
          status: 'FAILED',
          attempts: attempt,
          lastError: message,
          nextAttemptAt: new Date(Date.now() + backoffDelay(attempt)),
        },
      });
    } else {
      await prisma.delivery.update({
        where: { id: delivery.id },
        data: { status: 'DEAD', attempts: attempt, lastError: message },
      });
      logger.warn({ deliveryId, endpointId: endpoint.id }, 'delivery dead-lettered');
    }
  }
}
