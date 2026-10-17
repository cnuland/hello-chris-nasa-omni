// Minimal structured logger for the browser console (JSON-ish records with a component tag).
type Level = 'debug' | 'info' | 'warn' | 'error';

function emit(level: Level, msg: string, fields: Record<string, unknown> = {}): void {
  if (import.meta.env?.MODE === 'test' && level !== 'error') return;
  const record = { ts: new Date().toISOString(), level, component: 'dashboard', msg, ...fields };
  // eslint-disable-next-line no-console
  (console[level] ?? console.log)(JSON.stringify(record));
}

export const log = {
  debug: (msg: string, f?: Record<string, unknown>) => emit('debug', msg, f),
  info: (msg: string, f?: Record<string, unknown>) => emit('info', msg, f),
  warn: (msg: string, f?: Record<string, unknown>) => emit('warn', msg, f),
  error: (msg: string, f?: Record<string, unknown>) => emit('error', msg, f),
};
