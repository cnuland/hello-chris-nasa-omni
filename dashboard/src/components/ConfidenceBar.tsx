/** Confidence 0..1 rendered as an accessible progressbar (aria-valuenow is a 0-100 percent). */
export function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100);
  const level = pct >= 75 ? 'high' : pct >= 50 ? 'mid' : 'low';
  return (
    <div className="conf">
      <div
        className={`conf-bar conf-${level}`}
        role="progressbar"
        aria-label="Confidence"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <span style={{ width: `${pct}%` }} />
      </div>
      <span className="conf-pct" aria-hidden="true">{`${pct}%`}</span>
    </div>
  );
}
