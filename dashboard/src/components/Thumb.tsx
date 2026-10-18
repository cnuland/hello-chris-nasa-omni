import { mediaUrl } from '../lib/format';
import { safePersonImage } from '../lib/person';

/**
 * Image served through /api/media, or a neutral placeholder (never an <img> with a bogus src). For a person
 * (UI3-03) only scene frames are shown: a crop / exemplar / face URL becomes the placeholder and is never requested.
 */
export function Thumb({ src, alt, size = 'sm', person = false }: { src?: string | null; alt: string; size?: 'sm' | 'lg' | 'md'; person?: boolean }) {
  const url = mediaUrl(person ? safePersonImage(src) : src);
  if (!url) {
    return (
      <div className={`thumb thumb-${size} thumb-empty`} aria-hidden="true">
        no image
      </div>
    );
  }
  return <img className={`thumb thumb-${size}`} src={url} alt={alt} loading="lazy" />;
}
