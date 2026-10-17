import { mediaUrl } from '../lib/format';

/** Image served through /api/media, or a neutral placeholder (never an <img> with a bogus src). */
export function Thumb({ src, alt, size = 'sm' }: { src?: string | null; alt: string; size?: 'sm' | 'lg' | 'md' }) {
  const url = mediaUrl(src);
  if (!url) {
    return (
      <div className={`thumb thumb-${size} thumb-empty`} aria-hidden="true">
        no image
      </div>
    );
  }
  return <img className={`thumb thumb-${size}`} src={url} alt={alt} loading="lazy" />;
}
