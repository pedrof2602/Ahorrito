import { useEffect, useState } from 'react';
import { MapPin, Plus, Store, Trash2 } from 'lucide-react';
import { fetchChains } from '../lib/api';
import { useCustomStores } from '../hooks/useCustomStores';
import { AddressLocator } from './AddressLocator';
import { Sheet } from './ui/Sheet';
import './StoresSheet.css';

const EMPTY_FORM = {
  chain_slug: '',
  name: '',
  address: '',
  city: '',
  province: '',
  latitude: null,
  longitude: null,
  geo_label: null,
};

/**
 * Los súper que tenés cerca, cargados a mano.
 *
 * Sirve para dos cosas que la carga automática no resuelve. Una: sucursales que
 * la cadena no publica con coordenadas —Coto publica direcciones en texto, y
 * geocodificarlas le erra seguido de partido— y que si no quedan afuera del
 * mapa. Otra: corregir una que quedó mal ubicada, sin esperar a que la arregle
 * nadie.
 *
 * Por eso una sucursal cargada acá **le gana a la automática de la misma
 * cadena** al elegir la más cercana, aunque quede unos metros más lejos: la
 * cargaste justamente porque la automática está mal o no existe.
 *
 * **No cambia ningún precio.** El total que muestra el marker sigue siendo el
 * de la cadena, calculado igual que en la tabla. Cargar la sucursal de la
 * esquina no hace que la app conozca los precios de esa sucursal — la mayoría
 * de las cadenas publica precio nacional y eso no depende de nosotros.
 */
export function StoresSheet({ onClose, onChanged }) {
  const custom = useCustomStores(true);
  const [chains, setChains] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [adding, setAdding] = useState(false);
  const [formError, setFormError] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchChains()
      .then(setChains)
      .catch(() => setChains([]));
  }, []);

  const field = (key) => ({
    value: form[key] ?? '',
    onChange: (e) => setForm({ ...form, [key]: e.target.value }),
  });

  const close = () => {
    setAdding(false);
    setForm(EMPTY_FORM);
    setFormError(null);
  };

  const submit = async (event) => {
    event.preventDefault();
    setFormError(null);
    if (form.latitude == null) {
      // El backend también lo rechaza; acá se corta antes para no gastar un
      // viaje de ida y vuelta en decir algo que ya se sabe.
      setFormError('Ubicá la sucursal antes de guardarla.');
      return;
    }
    setSaving(true);
    try {
      await custom.create({
        chain_slug: form.chain_slug,
        name: form.name.trim(),
        latitude: form.latitude,
        longitude: form.longitude,
        address: form.address.trim() || null,
        city: form.city.trim() || null,
        province: form.province.trim() || null,
      });
      close();
      // El mapa tiene sus ubicaciones cacheadas: sin avisarle, la sucursal
      // recién cargada no aparecería hasta recargar la página.
      onChanged?.();
    } catch (cause) {
      setFormError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id) => {
    await custom.remove(id);
    onChanged?.();
  };

  return (
    <Sheet
      title="Mis sucursales"
      onClose={onClose}
      footer={
        adding ? null : (
          <button
            className="btn btn--primary btn--block"
            onClick={() => setAdding(true)}
          >
            <Plus size={18} strokeWidth={2} aria-hidden />
            Agregar sucursal
          </button>
        )
      }
    >
      {custom.status === 'loading' ? <p className="cstore-note">Cargando…</p> : null}
      {custom.error ? <p className="cstore-error">{custom.error}</p> : null}

      {custom.status === 'success' && !custom.stores.length && !adding ? (
        <p className="cstore-note">
          Cargá los súper que tenés cerca y el mapa los va a usar en lugar de
          los que ubicó solo. Sirve sobre todo para las sucursales que la cadena
          no publica con coordenadas, y para corregir alguna que quedó en la
          cuadra equivocada. Los precios no cambian: siguen siendo los de la
          cadena.
        </p>
      ) : null}

      <ul className="cstore-list">
        {custom.stores.map((store) => (
          <li className="cstore" key={store.id}>
            <div className="cstore-main">
              <p className="cstore-name">
                <Store size={13} strokeWidth={2} aria-hidden />
                {store.name}
              </p>
              <p className="cstore-chain">{store.chain_name}</p>
              {store.address ? (
                <p className="cstore-meta">
                  {[store.address, store.city].filter(Boolean).join(', ')}
                </p>
              ) : null}
              <p className="cstore-meta num">
                {store.latitude.toFixed(5)}, {store.longitude.toFixed(5)}
              </p>
            </div>
            <button
              className="btn btn--icon"
              onClick={() => remove(store.id)}
              disabled={custom.busyId === store.id}
              aria-label={`Borrar ${store.name}`}
            >
              <Trash2 size={16} strokeWidth={1.75} aria-hidden />
            </button>
          </li>
        ))}
      </ul>

      {adding ? (
        <form className="cstore-form" onSubmit={submit}>
          <div className="field">
            <label className="field-label" htmlFor="cstore-chain">
              Cadena
            </label>
            <select
              id="cstore-chain"
              className="input"
              required
              autoFocus
              {...field('chain_slug')}
            >
              <option value="">Elegí una…</option>
              {chains.map((chain) => (
                <option key={chain.slug} value={chain.slug}>
                  {chain.display_name}
                </option>
              ))}
            </select>
            {/* Es la restricción que sostiene que todo marker del mapa tenga un
                total al lado. Decirlo acá evita que alguien busque un Chango Más
                que no está y concluya que la lista se cargó mal. */}
            <p className="cstore-hint">
              Solo las cadenas que la app compara: un súper sin precios dejaría
              un marker que no se puede comparar con los otros.
            </p>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="cstore-name">
              Nombre
            </label>
            <input
              id="cstore-name"
              className="input"
              {...field('name')}
              placeholder="El de la esquina"
              required
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="cstore-address">
              Dirección
            </label>
            <input
              id="cstore-address"
              className="input"
              {...field('address')}
              placeholder="Gallo 250"
            />
          </div>

          <div className="cstore-row">
            <div className="field">
              <label className="field-label" htmlFor="cstore-city">
                Partido o comuna
              </label>
              <input
                id="cstore-city"
                className="input"
                {...field('city')}
                placeholder="Vicente López"
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="cstore-province">
                Provincia
              </label>
              <input
                id="cstore-province"
                className="input"
                {...field('province')}
                placeholder="Buenos Aires"
              />
            </div>
          </div>

          <div className="field">
            <span className="field-label">
              <MapPin size={12} strokeWidth={2} aria-hidden /> Ubicación
            </span>
            <AddressLocator
              value={form}
              address={form.address}
              city={form.city}
              province={form.province}
              onPick={(spot) => setForm({ ...form, ...spot })}
              onClear={() =>
                setForm({
                  ...form,
                  latitude: null,
                  longitude: null,
                  geo_label: null,
                })
              }
            />
          </div>

          {formError ? <p className="field-error">{formError}</p> : null}

          <div className="cstore-form-actions">
            <button type="button" className="btn btn--ghost" onClick={close}>
              Cancelar
            </button>
            <button className="btn btn--primary" disabled={saving}>
              {saving ? 'Guardando…' : 'Guardar'}
            </button>
          </div>
        </form>
      ) : null}
    </Sheet>
  );
}
