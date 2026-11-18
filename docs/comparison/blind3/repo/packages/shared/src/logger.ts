import pino from 'pino';

// JSON to stdout; the log shipper parses every line as JSON.
export const logger = pino({
  level: process.env.LOG_LEVEL ?? 'info',
  base: { service: process.env.SERVICE_NAME ?? 'tallyhook' },
});

export type Logger = typeof logger;
