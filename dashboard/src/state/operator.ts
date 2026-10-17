// Shared operator name (review page, edit/add forms, chat confirm). Persisted in localStorage so every audited write
// from this browser is attributed to the same human; all mounted users of the hook stay in sync.
import { useCallback, useEffect, useState } from 'react';

export const OPERATOR_KEY = 'nasa-omni.operator';
const CHANGE_EVENT = 'nasa-omni:operator';

function read(): string {
  try {
    return window.localStorage.getItem(OPERATOR_KEY) ?? '';
  } catch {
    return '';
  }
}

/** Returns [operator (raw input), setOperator]. The trimmed value is persisted (cleared when empty). */
export function useOperator(): [string, (v: string) => void] {
  const [operator, setLocal] = useState(read);

  useEffect(() => {
    const sync = () => {
      const stored = read();
      setLocal((cur) => (cur.trim() === stored ? cur : stored));
    };
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  const setOperator = useCallback((v: string) => {
    setLocal(v);
    try {
      if (v.trim()) window.localStorage.setItem(OPERATOR_KEY, v.trim());
      else window.localStorage.removeItem(OPERATOR_KEY);
    } catch {
      /* storage unavailable: keep in memory only */
    }
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);

  return [operator, setOperator];
}
