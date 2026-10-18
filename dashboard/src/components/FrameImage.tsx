// AC-UI-14: evidence image with an optional bounding-box overlay and click-to-enlarge (accessible lightbox).
// The box is positioned with percentages through React's style prop (CSSOM), which the CSP (style-src 'self')
// allows; no inline <style> or style attribute strings are emitted.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import type { BBox, BBoxSource } from '../api/types';
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

/** Share of the box width/height added on each side of an approximate (omni) box: coarse boxes often miss (UI2-07). */
export const APPROX_PAD = 0.15;

/** The box as drawn: omni boxes are padded by APPROX_PAD per side (clamped to the frame); others unchanged. */
export function displayBox(box: BBox, source: BBoxSource): BBox {
  if (source !== 'omni') return box;
  const [x1, y1, x2, y2] = box;
  const dx = (x2 - x1) * APPROX_PAD;
  const dy = (y2 - y1) * APPROX_PAD;
  return [Math.max(0, x1 - dx), Math.max(0, y1 - dy), Math.min(1, x2 + dx), Math.min(1, y2 + dy)];
}

/** Caption under a frame: what the box means, or that the image is the whole frame (UI2-07 / UI-08). */
export function boxCaption(box: BBox | null, source: BBoxSource, compact = false): string {
  if (!box) return compact ? 'Whole frame (no box)' : 'Whole frame: there is no box for this object. Click to enlarge.';
  if (source === 'omni') {
    return compact ? 'Approximate box' : 'The dashed box is approximate (a model estimate). Click to enlarge.';
  }
  return compact ? 'Box marks the object' : 'The outlined box marks the object. Click to enlarge.';
}

const pct = (v: number) => `${+(v * 100).toFixed(2)}%`;

function BoxOverlay({ box, source }: { box: BBox; source: BBoxSource }) {
  // The "approximate" tag is CSS generated content (.bbox-approx::after): no text node inside the enlarge button.
  const approx = source === 'omni';
  const [x1, y1, x2, y2] = displayBox(box, source);
  const cls = approx ? 'bbox bbox-approx' : source === 'detector' ? 'bbox bbox-detector' : 'bbox';
  return (
    <span
      className={cls}
      data-testid="bbox"
      aria-hidden="true"
      style={{ left: pct(x1), top: pct(y1), width: pct(x2 - x1), height: pct(y2 - y1) }}
    />
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

interface LightboxProps {
  src: string;
  alt: string;
  box: BBox | null;
  source?: BBoxSource;
  onClose: () => void;
}

/** Modal image viewer: role=dialog + aria-modal, focus moves in and is trapped, Esc / backdrop / Close dismiss. */
export function Lightbox({ src, alt, box, source, onClose }: LightboxProps) {
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
          <span className="muted">{`${boxCaption(box, source).replace(' Click to enlarge.', '')} Press Esc to close.`}</span>
          <button ref={closeRef} type="button" onClick={onClose}>Close</button>
        </div>
        <span className="frame frame-full">
          <img src={src} alt={alt} />
          {box && <BoxOverlay box={box} source={source} />}
        </span>
      </div>
    </div>
  );
}

interface FrameImageProps {
  src?: string | null;
  alt: string;
  bbox?: unknown;
  /** Where the box came from: detector = solid, omni = dashed "approximate", unknown = solid (older APIs). */
  bboxSource?: BBoxSource;
  size?: 'md' | 'review' | 'lg';
  /** Show a caption saying what the box means or "whole frame" ('compact' for small tiles). */
  caption?: boolean | 'compact';
}

/** Thumbnail-sized frame (bbox drawn when given) that opens the Lightbox; focus returns to it on close. */
export function FrameImage({ src, alt, bbox, bboxSource, size = 'review', caption = false }: FrameImageProps) {
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
      <button ref={triggerRef} type="button" className={`frame-trigger frame-${size}`} aria-label={`Enlarge image: ${alt}${box && bboxSource === 'omni' ? ' (approximate box)' : ''}`}
        aria-haspopup="dialog" onClick={() => setOpen(true)}>
        <span className="frame">
          <img src={url} alt={alt} loading="lazy" />
          {box && <BoxOverlay box={box} source={bboxSource} />}
        </span>
      </button>
      {caption && <p className="frame-caption muted small">{boxCaption(box, bboxSource, caption === 'compact')}</p>}
      {open && <Lightbox src={url} alt={alt} box={box} source={bboxSource} onClose={close} />}
    </>
  );
}
