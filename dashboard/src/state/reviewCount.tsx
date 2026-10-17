// Open review-queue count for the nav badge (AC-UI-7), from GET /stats open_reviews_by_kind, polled every 30 s. Failures are silent
// here (pages that need stats report their own errors).
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { getStats } from '../api/client';
import { log } from '../lib/log';

export const REVIEW_COUNT_REFRESH_MS = 30_000;

interface ReviewCountApi {
  count: number | null;
  refresh: () => void;
}

const ReviewCountContext = createContext<ReviewCountApi>({ count: null, refresh: () => {} });

export function ReviewCountProvider({ children }: { children: ReactNode }) {
  const [count, setCount] = useState<number | null>(null);

  const seq = useRef(0);
  const refresh = useCallback(() => {
    const mine = ++seq.current; // only the latest request may set the count (no stale overwrite)
    getStats()
      .then((s) => mine === seq.current && setCount(Object.values(s.open_reviews_by_kind ?? {}).reduce((a, b) => a + (Number(b) || 0), 0)))
      .catch((e) => log.debug('review count unavailable', { error: String(e) }));
  }, []);

  // Refreshed on mount, periodically (new unknowns from perception), and on demand (resolve, review page mount).
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REVIEW_COUNT_REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const api = useMemo(() => ({ count, refresh }), [count, refresh]);
  return <ReviewCountContext.Provider value={api}>{children}</ReviewCountContext.Provider>;
}

export const useReviewCount = () => useContext(ReviewCountContext);
