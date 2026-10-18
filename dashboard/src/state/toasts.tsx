// Non-blocking toast notifications (AC-UI-10). Errors use role="alert" and stay until dismissed; success/info
// toasts auto-dismiss. Identical messages already on screen are not duplicated.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '../api/client';

export type ToastKind = 'error' | 'success' | 'info';
/** Optional extras (UI2-05): an in-app link (e.g. the merge target) and one action button (e.g. Undo). */
export interface ToastOptions {
  link?: { to: string; label: string };
  action?: { label: string; onClick: () => void };
}

export interface Toast extends ToastOptions {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  push: (kind: ToastKind, message: string, opts?: ToastOptions) => void;
  error: (context: string, err: unknown) => void;
  success: (message: string, opts?: ToastOptions) => void;
}

const AUTO_DISMISS_MS = 6000;
/** Toasts with an action (Undo) stay longer so the operator has time to use it. */
const ACTION_DISMISS_MS = 20000;
const ToastContext = createContext<ToastApi | null>(null);

/** Formats any thrown value for display ("Could not load items: database unavailable"). */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer) clearTimeout(timer);
    timers.current.delete(id);
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string, opts: ToastOptions = {}) => {
      const id = nextId.current++;
      setToasts((ts) => (ts.some((t) => t.kind === kind && t.message === message) ? ts : [...ts, { id, kind, message, ...opts }]));
      if (kind !== 'error') timers.current.set(id, setTimeout(() => dismiss(id), opts.action ? ACTION_DISMISS_MS : AUTO_DISMISS_MS));
    },
    [dismiss],
  );

  useEffect(() => {
    const map = timers.current;
    return () => {
      for (const t of map.values()) clearTimeout(t);
      map.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      push,
      error: (context, err) => push('error', `${context}: ${errorMessage(err)}`),
      success: (message, opts) => push('success', message, opts),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <section className="toasts" role="region" aria-label="Notifications">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} role={t.kind === 'error' ? 'alert' : 'status'}>
            <div className="toast-body">
              <p className="toast-message">{t.message}</p>
              {(t.link || t.action) && (
                <div className="toast-actions">
                  {t.link && <Link to={t.link.to} onClick={() => dismiss(t.id)}>{t.link.label}</Link>}
                  {t.action && (
                    <button type="button" onClick={() => {
                      dismiss(t.id);
                      t.action?.onClick();
                    }}>
                      {t.action.label}
                    </button>
                  )}
                </div>
              )}
            </div>
            <button type="button" className="toast-close" aria-label="Dismiss notification" onClick={() => dismiss(t.id)}>
              ×
            </button>
          </div>
        ))}
      </section>
    </ToastContext.Provider>
  );
}

export function useToasts(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToasts must be used inside <ToastProvider>');
  return ctx;
}
