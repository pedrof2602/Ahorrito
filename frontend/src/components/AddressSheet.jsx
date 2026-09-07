import { useState } from 'react';
import { Home, MapPin, Plus, Star, Trash2 } from 'lucide-react';
import { useAddresses } from '../hooks/useAddresses';
import { AddressLocator } from './AddressLocator';
import { Sheet } from './ui/Sheet';
import './AddressSheet.css';

const EMPTY_FORM = {
  label: '',
  street: '',
  number: '',
  floor: '',
  apartment: '',
  city: '',
  province: '',
  postal_code: '',
  notes: '',
  is_default: false,
  latitude: null,
  longitude: null,
  geo_source: null,
  geo_label: null,
};

/** «Av. Corrientes 1234, 3° B» a partir de los campos que estén cargados. */
function formatStreet({ street, number, floor, apartment }) {
  const line = [street, number].filter(Boolean).join(' ');
  const unit = [floor && `${floor}°`, apartment].filter(Boolean).join(' ');
  return [line, unit].filter(Boolean).join(', ');
}

/** Lo que se le pasa al geocodificador: calle y altura, sin piso ni depto. */
function streetQuery({ street, number }) {
  return [street, number].filter(Boolean).join(' ').trim();
}

/**
 * Mis domicilios.
 *
 * El código postal de acá **no** es el que resuelve la sucursal: ese está en
 * los ajustes. Guardar la dirección del trabajo no tiene por qué cambiar los
 * precios que ves.
 *
 * Lo que sí cambia un domicilio **ubicado** es el mapa: pasa a ser el punto
 * desde el que se mide la cercanía, en lugar del promedio de las sucursales de
 * tu código postal. La diferencia es concreta: un CP de CABA abarca un área
 * donde entran tres sucursales de la misma cadena, así que sin la dirección
 * exacta «la más cercana» puede ser una que te queda a quince cuadras de la que
 * tenés en la otra vereda.
 */
export function AddressSheet({ onClose }) {
  const addresses = useAddresses(true);
  const [form, setForm] = useState(EMPTY_FORM);
  const [adding, setAdding] = useState(false);
  const [formError, setFormError] = useState(null);
  const [saving, setSaving] = useState(false);
  // Qué domicilio ya guardado se está ubicando. Uno por vez: dos localizadores
  // abiertos son dos búsquedas compitiendo por la misma pantalla.
  const [locating, setLocating] = useState(null);

  const field = (key) => ({
    value: form[key],
    onChange: (e) => setForm({ ...form, [key]: e.target.value }),
  });

  const submit = async (event) => {
    event.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      await addresses.create({
        ...form,
        label: form.label.trim(),
        // Los campos vacíos viajan como null: el backend no distingue entre
        // «lo borré» y «nunca lo cargué», y mandar "" guardaría un hueco.
        street: form.street.trim() || null,
        number: form.number.trim() || null,
        floor: form.floor.trim() || null,
        apartment: form.apartment.trim() || null,
        city: form.city.trim() || null,
        province: form.province.trim() || null,
        postal_code: form.postal_code.trim() || null,
        notes: form.notes.trim() || null,
      });
      setForm(EMPTY_FORM);
      setAdding(false);
    } catch (cause) {
      setFormError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Sheet
      title="Mis domicilios"
      onClose={onClose}
      footer={
        adding ? null : (
          <button
            className="btn btn--primary btn--block"
            onClick={() => setAdding(true)}
          >
            <Plus size={18} strokeWidth={2} aria-hidden />
            Agregar domicilio
          </button>
        )
      }
    >
      {addresses.status === 'loading' ? (
        <p className="addr-note">Cargando…</p>
      ) : null}
      {addresses.error ? <p className="addr-error">{addresses.error}</p> : null}

      {addresses.status === 'success' && !addresses.addresses.length && !adding ? (
        <p className="addr-note">
          Todavía no guardaste ninguno. Son para tenerlos a mano; el código
          postal que decide de qué sucursal salen los precios se carga en los
          ajustes, no acá.
        </p>
      ) : null}

      <ul className="addr-list">
        {addresses.addresses.map((address) => (
          <li className="addr" key={address.id}>
            <div className="addr-main">
              <p className="addr-label">{address.label}</p>
              {formatStreet(address) ? (
                <p className="addr-street">{formatStreet(address)}</p>
              ) : null}
              <p className="addr-meta">
                {[address.city, address.province].filter(Boolean).join(', ')}
                {address.postal_code ? ` · CP ${address.postal_code}` : ''}
              </p>
              {address.notes ? <p className="addr-meta">{address.notes}</p> : null}
              {address.is_default || address.latitude != null ? (
                <div className="addr-tags">
                  {address.is_default ? (
                    <span className="chip chip--win">
                      <Star size={11} strokeWidth={2.5} aria-hidden />
                      principal
                    </span>
                  ) : null}
                  {address.latitude != null ? (
                    <span className="chip">
                      <MapPin size={11} strokeWidth={2.5} aria-hidden />
                      ubicada
                    </span>
                  ) : null}
                </div>
              ) : null}

              {locating === address.id ? (
                <div className="addr-locator">
                  <AddressLocator
                    value={address}
                    address={streetQuery(address)}
                    city={address.city}
                    province={address.province}
                    onPick={async (spot) => {
                      await addresses.update(address.id, spot);
                      setLocating(null);
                    }}
                    onClear={() =>
                      addresses.update(address.id, {
                        latitude: null,
                        longitude: null,
                        geo_source: null,
                        geo_label: null,
                      })
                    }
                  />
                </div>
              ) : null}
            </div>

            <div className="addr-actions">
              <button
                className="btn btn--icon"
                onClick={() =>
                  setLocating(locating === address.id ? null : address.id)
                }
                aria-pressed={locating === address.id}
                aria-label={`Ubicar ${address.label} en el mapa`}
              >
                <MapPin size={16} strokeWidth={1.75} aria-hidden />
              </button>
              {address.is_default ? null : (
                <button
                  className="btn btn--icon"
                  onClick={() => addresses.update(address.id, { is_default: true })}
                  disabled={addresses.busyId === address.id}
                  aria-label={`Marcar ${address.label} como principal`}
                >
                  <Star size={16} strokeWidth={1.75} aria-hidden />
                </button>
              )}
              <button
                className="btn btn--icon"
                onClick={() => addresses.remove(address.id)}
                disabled={addresses.busyId === address.id}
                aria-label={`Borrar ${address.label}`}
              >
                <Trash2 size={16} strokeWidth={1.75} aria-hidden />
              </button>
            </div>
          </li>
        ))}
      </ul>

      {adding ? (
        <form className="addr-form" onSubmit={submit}>
          <div className="field">
            <label className="field-label" htmlFor="addr-label">
              Nombre
            </label>
            <input
              id="addr-label"
              className="input"
              {...field('label')}
              placeholder="Casa"
              required
              autoFocus
            />
          </div>

          <div className="addr-row">
            <div className="field">
              <label className="field-label" htmlFor="addr-street">
                Calle
              </label>
              <input
                id="addr-street"
                className="input"
                {...field('street')}
                placeholder="Av. Corrientes"
                autoComplete="address-line1"
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="addr-number">
                Altura
              </label>
              <input
                id="addr-number"
                className="input num"
                {...field('number')}
                placeholder="1234"
              />
            </div>
          </div>

          <div className="addr-row addr-row--even">
            <div className="field">
              <label className="field-label" htmlFor="addr-floor">
                Piso
              </label>
              <input id="addr-floor" className="input" {...field('floor')} placeholder="3" />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="addr-apt">
                Depto
              </label>
              <input id="addr-apt" className="input" {...field('apartment')} placeholder="B" />
            </div>
          </div>

          <div className="addr-row addr-row--even">
            <div className="field">
              <label className="field-label" htmlFor="addr-city">
                Localidad
              </label>
              <input
                id="addr-city"
                className="input"
                {...field('city')}
                placeholder="CABA"
                autoComplete="address-level2"
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="addr-cp">
                Código postal
              </label>
              <input
                id="addr-cp"
                className="input num"
                {...field('postal_code')}
                placeholder="1425"
                inputMode="numeric"
                autoComplete="postal-code"
              />
            </div>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="addr-province">
              Provincia
            </label>
            <input
              id="addr-province"
              className="input"
              {...field('province')}
              placeholder="Buenos Aires"
              autoComplete="address-level1"
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="addr-notes">
              Referencias
            </label>
            <input
              id="addr-notes"
              className="input"
              {...field('notes')}
              placeholder="Timbre roto, tocar el de al lado"
            />
          </div>

          <div className="field">
            <span className="field-label">Ubicación</span>
            <AddressLocator
              value={form}
              address={streetQuery(form)}
              city={form.city}
              province={form.province}
              onPick={(spot) => setForm({ ...form, ...spot })}
              onClear={() =>
                setForm({
                  ...form,
                  latitude: null,
                  longitude: null,
                  geo_source: null,
                  geo_label: null,
                })
              }
            />
            <p className="addr-hint">
              Opcional. Con la ubicación cargada, el mapa mide la cercanía desde
              tu puerta en vez de desde el centro de tu código postal.
            </p>
          </div>

          <div className="field">
            <button
              type="button"
              className={`chip addr-toggle${form.is_default ? ' addr-toggle--on' : ''}`}
              aria-pressed={form.is_default}
              onClick={() => setForm({ ...form, is_default: !form.is_default })}
            >
              <Home size={12} strokeWidth={2} aria-hidden />
              Principal
            </button>
          </div>

          {formError ? <p className="field-error">{formError}</p> : null}

          <div className="addr-form-actions">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => {
                setAdding(false);
                setFormError(null);
              }}
            >
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
