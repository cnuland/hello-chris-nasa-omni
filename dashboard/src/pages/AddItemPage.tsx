// AC-UI-6: add an item. An image is required (submit disabled + message until an image file is chosen); posts
// multipart/form-data to POST /items.
import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { createItem } from '../api/client';
import { LocationOptions } from '../components/LocationSelect';
import { useCatalog, useLocations } from '../state/reference';
import { useOperator } from '../state/operator';
import { useToasts } from '../state/toasts';

export default function AddItemPage() {
  const toasts = useToasts();
  const navigate = useNavigate();
  const { options: locationOptions } = useLocations();
  const catalog = useCatalog();

  const [image, setImage] = useState<File | null>(null);
  const [imageRejected, setImageRejected] = useState(false);
  const [label, setLabel] = useState('');
  const [location, setLocation] = useState('');
  const [catalogId, setCatalogId] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [operator, setOperator] = useOperator();

  const preview = useMemo(() => (image ? URL.createObjectURL(image) : null), [image]);
  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview);
  }, [preview]);

  const onImage = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null;
    if (f && !f.type.startsWith('image/')) {
      setImage(null);
      setImageRejected(true);
      return;
    }
    setImageRejected(false);
    setImage(f);
  };

  const valid = !!image && label.trim() !== '' && location !== '';

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid || submitting || !image) return;
    const form = new FormData();
    form.append('image', image, image.name);
    form.append('label', label.trim());
    form.append('location_id', location);
    if (catalogId) form.append('catalog_id', catalogId);
    if (description.trim()) form.append('description', description.trim());
    if (operator.trim()) form.append('operator', operator.trim());
    setSubmitting(true);
    try {
      const created = await createItem(form);
      toasts.success(`Item added: ${created.label}`);
      navigate(`/items/${created.entity_id}`);
    } catch (err) {
      toasts.error('Could not add item', err);
      setSubmitting(false);
    }
  };

  return (
    <div className="page add">
      <div className="page-head">
        <div>
          <Link to="/" className="muted">← Inventory</Link>
          <h1>Add item</h1>
        </div>
      </div>
      <form className="card form" aria-label="Add item" noValidate onSubmit={submit}>
        <div className="field">
          <label htmlFor="a-image">Image</label>
          <input id="a-image" type="file" accept="image/*" aria-describedby="a-image-msg" required onChange={onImage} />
          {!image && (
            <p id="a-image-msg" className="field-error">
              {imageRejected ? 'Image is required: the chosen file is not an image.' : 'Image is required.'}
            </p>
          )}
          {preview && <img className="thumb thumb-md" src={preview} alt="Selected image preview" />}
        </div>
        <div className="grid2">
          <div className="field">
            <label htmlFor="a-label">Label</label>
            <input id="a-label" value={label} required onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="a-location">Location</label>
            <select id="a-location" value={location} required onChange={(e) => setLocation(e.target.value)}>
              <option value="">Choose a location…</option>
              <LocationOptions options={locationOptions} />
            </select>
          </div>
          <div className="field">
            <label htmlFor="a-catalog">Catalog</label>
            <select id="a-catalog" value={catalogId} onChange={(e) => setCatalogId(e.target.value)}>
              <option value="">None (optional)</option>
              {catalog.map((c) => <option key={c.catalog_id} value={c.catalog_id}>{c.display_name}</option>)}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="a-desc">Description</label>
          <textarea id="a-desc" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="a-operator">Operator</label>
          <input id="a-operator" value={operator} autoComplete="name" onChange={(e) => setOperator(e.target.value)} />
        </div>
        <div className="actions">
          <button type="submit" className="primary" disabled={!valid || submitting}>Add item</button>
        </div>
      </form>
    </div>
  );
}
