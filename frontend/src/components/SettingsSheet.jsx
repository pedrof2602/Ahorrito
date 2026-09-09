import { useEffect, useState } from 'react';
import { Copy, LogOut, MapPin, Mic, Store, Trash2 } from 'lucide-react';
import {
  createVoiceToken,
  deleteVoiceToken,
  fetchChains,
  fetchVoiceTokens,
} from '../lib/api';
import { useAuth } from '../lib/authContext';
import { Sheet } from './ui/Sheet';
import './SettingsSheet.css';

/**
 * Código postal, canal, tema y tus datos.
 *
 * El CP y el canal no son preferencias: el CP resuelve la sucursal en las
 * cadenas que regionalizan, y muchas promos bancarias son solo presenciales o
 * solo online, así que el canal cambia el resultado. Por eso el panel avisa que
 * hay que volver a comparar cuando se tocan — y por eso «Mis datos» va al final:
 * nada de lo que hay ahí mueve un precio.
 */
export function SettingsSheet({
  settings,
  onUpdate,
  onClose,
  onApply,
  onOpenAddresses,
  onOpenStores,
}) {
  const [chains, setChains] = useState([]);
  const [draft, setDraft] = useState(settings);
  const [tokens, setTokens] = useState([]);
  const [nuevo, setNuevo] = useState(null);
  const { user, logout } = useAuth();

  useEffect(() => {
    fetchChains()
      .then(setChains)
      .catch(() => setChains([])); // catálogo opcional: sin él se usa el default
  }, []);

  // El mismo `.catch()` silencioso: si los tokens no cargan, el panel sigue
  // sirviendo para todo lo demás, que es a lo que vino la mayoría.
  useEffect(() => {
    fetchVoiceTokens()
      .then(setTokens)
      .catch(() => setTokens([]));
  }, []);

  const crearToken = async () => {
    try {
      const creado = await createVoiceToken('iPhone');
      // Se guarda aparte del listado: es el único momento en que el valor en
      // claro existe de este lado, y la pantalla tiene que mostrarlo ahora o
      // nunca. El listado que vuelve del server ya no lo trae.
      setNuevo(creado);
      setTokens([creado, ...tokens]);
    } catch {
      // Sin cartel: el botón sigue ahí y se puede reintentar.
    }
  };

  const revocarToken = async (id) => {
    try {
      await deleteVoiceToken(id);
      setTokens(tokens.filter((t) => t.id !== id));
      if (nuevo?.id === id) setNuevo(null);
    } catch {
      // Ídem.
    }
  };

  const salesChannels = [
    ...new Set(chains.flatMap((chain) => chain.sales_channels ?? [])),
  ].sort((a, b) => a - b);

  const changed =
    draft.postalCode !== settings.postalCode ||
    draft.channel !== settings.channel ||
    draft.salesChannel !== settings.salesChannel;

  const save = () => {
    onUpdate(draft);
    onClose();
    if (changed) onApply();
  };

  return (
    <Sheet
      title="Contexto de la compra"
      onClose={onClose}
      footer={
        <button className="btn btn--primary btn--block" onClick={save}>
          {changed ? 'Guardar y comparar' : 'Listo'}
        </button>
      }
    >
      <div className="settings">
        <div className="field">
          <label className="field-label" htmlFor="cp">
            Código postal
          </label>
          <input
            id="cp"
            className="input num"
            value={draft.postalCode}
            onChange={(event) =>
              setDraft({ ...draft, postalCode: event.target.value.trim() })
            }
            placeholder="1425"
            inputMode="numeric"
            autoComplete="postal-code"
          />
          <p className="field-hint">
            Resuelve la sucursal en las cadenas que regionalizan. Sin esto los
            precios son los del canal por defecto de cada cadena.
          </p>
        </div>

        <div className="field">
          <span className="field-label">Cómo vas a pagar</span>
          <div className="seg" role="radiogroup" aria-label="Canal">
            {[
              ['in_store', 'En el local'],
              ['online', 'Online'],
            ].map(([value, label]) => (
              <button
                key={value}
                role="radio"
                aria-checked={draft.channel === value}
                className={`seg-btn${
                  draft.channel === value ? ' seg-btn--on' : ''
                }`}
                onClick={() => setDraft({ ...draft, channel: value })}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="field-hint">
            Muchas promos bancarias corren solo en un canal, así que el total
            cambia según cuál elijas.
          </p>
        </div>

        {salesChannels.length ? (
          <div className="field">
            <label className="field-label" htmlFor="sc">
              Canal de venta
            </label>
            <select
              id="sc"
              className="select"
              value={draft.salesChannel ?? ''}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  salesChannel: event.target.value
                    ? Number(event.target.value)
                    : null,
                })
              }
            >
              <option value="">Automático</option>
              {salesChannels.map((channel) => (
                <option key={channel} value={channel}>
                  Canal {channel}
                </option>
              ))}
            </select>
            <p className="field-hint">
              Publican precios distintos para el mismo producto, pero solo el
              canal 1 surte de verdad: los otros muestran precios reales de cosas
              que casi nunca hay. Dejalo en automático salvo que sepas qué buscás.
            </p>
          </div>
        ) : null}

        <div className="field">
          <span className="field-label">Tema</span>
          <div className="seg" role="radiogroup" aria-label="Tema">
            {[
              ['system', 'Sistema'],
              ['light', 'Claro'],
              ['dark', 'Oscuro'],
            ].map(([value, label]) => (
              <button
                key={value}
                role="radio"
                aria-checked={settings.theme === value}
                className={`seg-btn${
                  settings.theme === value ? ' seg-btn--on' : ''
                }`}
                // El tema se aplica en el momento: es la única opción del panel
                // cuyo efecto se ve sin guardar, y esperar sería raro.
                onClick={() => onUpdate({ theme: value })}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* Nada de acá abajo cambia un precio: son datos para tenerlos guardados
            y que no se pierdan al cambiar de navegador. */}
        <div className="settings-group">
          <h3 className="settings-group-title">Mis datos</h3>

          <div className="field">
            <label className="field-label" htmlFor="full-name">
              Nombre
            </label>
            <input
              id="full-name"
              className="input"
              value={draft.fullName}
              onChange={(event) =>
                setDraft({ ...draft, fullName: event.target.value })
              }
              placeholder="Pedro Pérez"
              autoComplete="name"
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="dni">
              DNI
            </label>
            <input
              id="dni"
              className="input num"
              value={draft.dni}
              onChange={(event) => setDraft({ ...draft, dni: event.target.value })}
              placeholder="30123456"
              inputMode="numeric"
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              className="input"
              type="email"
              value={draft.email}
              onChange={(event) => setDraft({ ...draft, email: event.target.value })}
              placeholder="vos@ejemplo.com"
              autoComplete="email"
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="phone">
              Teléfono
            </label>
            <input
              id="phone"
              className="input"
              type="tel"
              value={draft.phone}
              onChange={(event) => setDraft({ ...draft, phone: event.target.value })}
              placeholder="+54 9 11 5555-5555"
              autoComplete="tel"
            />
          </div>

          {/* Se guarda antes de irse: salir del panel sin guardar tira el
              borrador, y perder lo que acabás de tipear por navegar a otra
              pantalla del mismo panel sería difícil de adivinar. */}
          <button
            className="btn btn--ghost btn--block"
            onClick={() => {
              onUpdate(draft);
              onOpenAddresses();
            }}
          >
            <MapPin size={16} strokeWidth={1.75} aria-hidden />
            Mis domicilios
          </button>

          {/* Va acá abajo, con «Mis datos», y no arriba con el CP: cargar una
              sucursal a mano cambia el mapa y no mueve ningún precio. */}
          <button
            className="btn btn--ghost btn--block"
            onClick={() => {
              onUpdate(draft);
              onOpenStores();
            }}
          >
            <Store size={16} strokeWidth={1.75} aria-hidden />
            Mis sucursales
          </button>

          {/* El atajo de voz va con «Mis datos» por lo mismo que las otras
              dos: no mueve un precio.

              Todo el bloque existe para una sola cosa: que el token se pueda
              copiar. Se muestra **una única vez**, al crearlo, porque de la base
              sólo se puede recuperar el hash. De ahí que el valor aparezca
              grande, seleccionable y con un botón de copiar en vez de escondido
              detrás de un "ver". */}
          <div className="settings-voz">
            <p className="settings-voz-title">
              <Mic size={16} strokeWidth={1.75} aria-hidden /> Agregar por voz
            </p>

            {nuevo ? (
              <div className="settings-voz-nuevo" role="status">
                <p className="settings-voz-aviso">
                  Copialo ahora: no se vuelve a mostrar.
                </p>
                <code className="settings-voz-token">{nuevo.token}</code>
                <button
                  className="btn btn--ghost btn--block"
                  onClick={() => navigator.clipboard?.writeText(nuevo.token)}
                >
                  <Copy size={16} strokeWidth={1.75} aria-hidden />
                  Copiar token
                </button>
              </div>
            ) : null}

            {tokens.length ? (
              <ul className="settings-voz-lista">
                {tokens.map((token) => (
                  <li key={token.id} className="settings-voz-item">
                    <span className="settings-voz-nombre">
                      {token.name || 'Sin nombre'}
                      {token.last_used_at ? (
                        <span className="settings-voz-uso">
                          {' · usado el '}
                          {new Date(token.last_used_at).toLocaleDateString('es-AR')}
                        </span>
                      ) : (
                        <span className="settings-voz-uso"> · sin usar</span>
                      )}
                    </span>
                    <button
                      className="settings-voz-borrar"
                      onClick={() => revocarToken(token.id)}
                      aria-label={`Revocar ${token.name || 'el token'}`}
                    >
                      <Trash2 size={15} strokeWidth={1.75} aria-hidden />
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}

            <button className="btn btn--ghost btn--block" onClick={crearToken}>
              Crear token para el atajo
            </button>
            <p className="field-hint">
              Con este token, un Atajo de Siri puede anotar en tu lista sin abrir
              la app: «Oye Siri, agregar a Ahorrito». No sirve para nada más —ni
              tus domicilios, ni tus medios de pago— así que si se te escapa,
              alcanza con revocarlo acá.
            </p>
          </div>

          {/* Último de todo y separado: cerrar sesión es la acción más
              destructiva del panel —te saca de la app— y no tiene por qué estar
              al alcance del pulgar de quien vino a cambiar el código postal. */}
          <div className="settings-account">
            <p className="settings-account-who">{user?.email}</p>
            <button className="btn btn--danger btn--block" onClick={logout}>
              <LogOut size={16} strokeWidth={1.75} aria-hidden />
              Cerrar sesión
            </button>
          </div>
        </div>
      </div>
    </Sheet>
  );
}
