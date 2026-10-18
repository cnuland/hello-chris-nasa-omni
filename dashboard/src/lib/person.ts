// UI review round 3: person display helpers.
//   UI3-03 a person is shown by appearance ("Person: black shirt, grey pants", else "Person <short id>"), and person
//          images are scene frames only (never a crop, exemplar or face URL);
//   UI3-05 readable duplicate reasons and appearance-attribute chips (colours + garments) for side-by-side comparison.
// Everything here returns plain text: callers render it as React text nodes, never as HTML.
import { shortId } from './format';

export const PERSON_CATALOG_ID = 'crew-member';
/** Longest appearance shown in a label before it is cut with "…". */
export const APPEARANCE_MAX = 40;

interface Entityish {
  entity_id: string;
  kind?: string | null;
  catalog_id?: string | null;
  label?: string | null;
  description?: string | null;
}

/** kind "person" or the crew-member catalog entry. */
export function isPerson(e: { kind?: string | null; catalog_id?: string | null } | null | undefined): boolean {
  if (!e) return false;
  return e.kind === 'person' || e.catalog_id === PERSON_CATALOG_ID;
}

const GENERIC = new Set([
  '', 'person', 'people', 'a person', 'crew member', 'crew-member', 'crewmember', 'crew', 'unknown person', 'unknown',
  'astronaut', 'human', 'someone', 'individual', 'figure',
]);
/** "(15 live 'person' entities would exceed the expected count of 7)": internal reconciler text. */
const INTERNAL_COUNT = /\s*\(\s*(\d+)\s+live\s+'?([^')]+?)'?\s+entities\s+would\s+exceed\s+the\s+expected\s+count\s+of\s+(\d+)\s*\)/i;

/**
 * The visible appearance from a description or label: drops a "person:" prefix, an internal count note, "wearing",
 * "person (…)" wrappers and trailing punctuation. '' when nothing descriptive is left.
 */
export function personAppearance(text: string | null | undefined): string {
  let s = (text ?? '').replace(new RegExp(INTERNAL_COUNT.source, 'gi'), '').replace(/\s+/g, ' ').trim();
  s = s.replace(/^(?:an?\s+|the\s+)?(?:person|crew ?member|crew-member|astronaut)\s*[:–—-]\s*/i, '');
  const wrapped = /^(?:an?\s+)?(?:person|crew ?member|astronaut)\s*\((.*)\)$/i.exec(s);
  if (wrapped) s = wrapped[1].trim();
  s = s.replace(/^(?:an?\s+)?(?:person|crew ?member|astronaut)\s+(?=wearing\b|in\b|with\b|dressed\b)/i, '');
  s = s.replace(/^(?:wearing|dressed in)\s+(?:an?\s+)?/i, '');
  s = s.replace(/[.;:,\s]+$/, '');
  if (/^[A-Z][a-z]/.test(s)) s = s[0].toLowerCase() + s.slice(1);
  return GENERIC.has(s.toLowerCase()) ? '' : s;
}

/** Appearance of an entity: its description, else a descriptive label. */
export function appearanceOf(e: { description?: string | null; label?: string | null }): string {
  return personAppearance(e.description) || personAppearance(e.label);
}

function clip(s: string, max = APPEARANCE_MAX): string {
  return s.length > max ? `${s.slice(0, max).trimEnd()}…` : s;
}

/** "Person: black shirt, grey pants" (cut at about 40 characters), else "Person 87dbd193". */
export function personLabel(e: Entityish): string {
  const a = appearanceOf(e);
  return a ? `Person: ${clip(a)}` : `Person ${shortId(e.entity_id)}`;
}

/** The label to show for any entity: the person label for persons, else the label, else "item <short id>". */
export function displayLabel(e: Entityish): string {
  if (isPerson(e)) return personLabel(e);
  return e.label?.trim() || `item ${shortId(e.entity_id)}`;
}

/** displayLabel plus the short id, without repeating the id when the label already ends with it. */
export function displayLabelWithId(e: Entityish): string {
  const label = displayLabel(e);
  const id = shortId(e.entity_id);
  return !id || label.endsWith(id) ? label : `${label} ${id}`;
}

/** Person images must be scene frames: crop, exemplar and face URLs are dropped (null). */
export function safePersonImage(url: string | null | undefined): string | null {
  if (!url) return null;
  return /\/(crops|exemplars|faces?)\//i.test(url) ? null : url;
}

/**
 * Review description for display: the internal expected-count note is removed from the text and turned into a
 * readable reason. Other descriptions are returned unchanged with reason null.
 */
export function reviewReason(description: string | null | undefined): { text: string; reason: string | null } {
  const d = description ?? '';
  const m = INTERNAL_COUNT.exec(d);
  if (!m) return { text: d, reason: null };
  const cls = m[2].trim().toLowerCase();
  const text = d.replace(m[0], ' ').replace(/\s+/g, ' ').trim();
  const reason = cls === 'person' || cls === PERSON_CATALOG_ID || cls === 'crew member'
    ? 'More person tracks than the expected crew size — these may be the same person'
    : `More ${cls} items than the expected count (${m[3]}) — some may be duplicates`;
  return { text, reason };
}

const COLOURS = new Set([
  'black', 'white', 'grey', 'gray', 'blue', 'navy', 'red', 'green', 'orange', 'yellow', 'purple', 'pink', 'brown', 'tan',
  'beige', 'khaki', 'maroon', 'teal', 'olive', 'silver', 'gold', 'cream', 'dark', 'light', 'pale', 'bright', 'blond',
  'blonde', 'striped', 'plaid',
]);
const SHAPES = new Set(['short', 'long', 'long-sleeve', 'long-sleeved', 'short-sleeve', 'short-sleeved', 'sleeveless', 'curly', 'tank']);
const GARMENTS = new Set([
  'shirt', 't-shirt', 'tshirt', 'tee', 'top', 'hoodie', 'sweater', 'sweatshirt', 'jacket', 'vest', 'polo', 'pants',
  'trousers', 'shorts', 'jeans', 'leggings', 'socks', 'shoes', 'sneakers', 'boots', 'hat', 'cap', 'beanie', 'helmet',
  'visor', 'headset', 'headphones', 'glasses', 'spacesuit', 'suit', 'overalls', 'coveralls', 'hair', 'beard', 'belt',
  'gloves', 'jumpsuit', 'uniform',
]);
export const MAX_CHIPS = 6;

/** "black shirt, grey pants" -> ["black shirt", "grey pants"]: each garment with the colour/shape words before it. */
export function appearanceChips(text: string | null | undefined): string[] {
  const out: string[] = [];
  const phrases = (text ?? '').toLowerCase().split(/[,;/]|\band\b|\bwith\b/);
  for (const phrase of phrases) {
    const words = phrase.replace(/[^a-z\s-]/g, ' ').split(/\s+/).filter(Boolean);
    words.forEach((w, i) => {
      if (!GARMENTS.has(w)) return;
      let j = i;
      while (j > 0 && (COLOURS.has(words[j - 1]) || SHAPES.has(words[j - 1]))) j -= 1;
      const chip = words.slice(j, i + 1).join(' ');
      if (!out.includes(chip)) out.push(chip);
    });
  }
  return out.slice(0, MAX_CHIPS);
}
