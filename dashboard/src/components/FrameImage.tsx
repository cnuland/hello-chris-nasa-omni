// AC-UI-14: evidence image with an optional bounding-box overlay and click-to-enlarge (accessible lightbox).
// The box is positioned with percentages through React's style prop (CSSOM), which the CSP (style-src 'self')
// allows; no inline <style> or style attribute strings are emitted.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import type { BBox } from '../api/types';
import { mediaUrl } from '../lib/format';

/** Validates a normalised [x1,y1,x2,y2] box; anything else (wrong length, NaN, out of range, empty) is ignored. */
export function validBBox(raw: unknown): BBox | null {
  if (!Array.isArray(raw) || raw.length !== 4) return null;
  const n = raw.map((v) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : NaN));
  if (n.some(Number.isNaN)) return null;
  const [x1, y1, x2, y2] = n;
  if (x2 - x1 <= 0.001 || y2 - y1 <= 0.001) return null;
  return [x1, y1, x2, y2];
}

function BoxOverlay({ box }: { box: BBox }) {
  const [x1, y1, x2, y2] = box;
  return (
    <span
      className="bbox"
      data-testid="bbox"
      aria-hidden="true"
      style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }}
    />
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

interface LightboxProps {
  src: string;
  alt: string;
  box: BBox | null;
  onClose: () => void;
}

/** Modal image viewer: role=dialog + aria-modal, focus moves in and is trapped, Esc / backdrop / Close dismiss. */
export function Lightbox({ src, alt, box, onClose }: LightboxProps) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== 'Tab' || !ref.current) return;
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  return (
    <div className="lightbox-backdrop" onClick={onClose}>
      <div
        ref={ref}
        className="lightbox"
        role="dialog"
        aria-modal="true"
        aria-label={`Enlarged image: ${alt}`}
        onKeyDown={onKeyDown}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="lightbox-bar">
          <span className="muted">{box ? 'The outlined box marks the object.' : 'Full frame.'} Press Esc to close.</span>
          <button ref={closeRef} type="button" onClick={onClose}>Close</button>
        </div>
        <span className="frame frame-full">
          <img src={src} alt={alt} />
          {box && <BoxOverlay box={box} />}
        </span>
      </div>
    </div>
  );
}

interface FrameImageProps {
  src?: string | null;
  alt: string;
  bbox?: unknown;
  size?: 'md' | 'review' | 'lg';
}

/** Thumbnail-sized frame (bbox drawn when given) that opens the Lightbox; focus returns to it on close. */
export function FrameImage({ src, alt, bbox, size = 'review' }: FrameImageProps) {
  const url = mediaUrl(src);
  const box = validBBox(bbox);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => {
    setOpen(false);
    // Return focus to the trigger (WAI-ARIA dialog pattern).
    setTimeout(() => triggerRef.current?.focus(), 0);
  }, []);
  if (!url) {
    return (
      <div className={`thumb thumb-${size === 'review' ? 'md' : size} thumb-empty`} aria-hidden="true">
        no image
      </div>
    );
  }
  return (
    <>
      <button ref={triggerRef} type="button" className={`frame-trigger frame-${size}`} aria-label={`Enlarge image: ${alt}`}
        aria-haspopup="dialog" onClick={() => setOpen(true)}>
        <span className="frame">
          <img src={url} alt={alt} loading="lazy" />
          {box && <BoxOverlay box={box} />}
        </span>
      </button>
      {open && <Lightbox src={url} alt={alt} box={box} onClose={close} />}
    </>
  );
}
