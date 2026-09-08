import { useEffect, useState } from 'react';
import { LogOut, MapPin, Mic, Store } from 'lucide-react';
import { fetchAlexaStatus, fetchChains, unlinkAlexa } from '../lib/api';
import { useAuth } from '../lib/authContext';
import { Sheet } from './ui/Sheet';
import './SettingsSheet.css';

/**
 * Qué decirle al usuario según cómo terminó el paseo por Amazon.
 *
 * El backend no manda texto: manda un código en `?alexa=` y lo traduce la app.
 * Es lo que permite que el callback —que es un redirect, no una respuesta que
 * alguien lea— no tenga que saber nada de cómo se escribe en esta pantalla.
 */
const ALEXA_MENSAJES = {
  ok: { tono: 'ok', texto: 'Listo: tu cuenta de Alexa quedó vinculada.' },
  cancelado: {
    tono: 'aviso',
    texto: 'No autorizaste el acceso en Amazon, así que no se vinculó nada.',
  },
  sesion: {
    tono: 'aviso',
    texto: 'Se venció la sesión durante el proceso. Probá de nuevo.',
  },
  sin_config: {
    tono: 'aviso',
    texto: 'El vínculo con Alexa no está configurado en este servidor.',
  },
  error: {
    tono: 'error',
    texto: 'No se pudo vincular la cuenta. Probá de nuevo en un rato.',
  },
};

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
  alexaOutcome = null,
}) {
  const [chains, setChains] = useState([]);
  const [draft, setDraft] = useState(settings);
  const [alexa, setAlexa] = useState(null);
  const { user, logout } = useAuth();

  useEffect(() => {
    fetchChains()
      .then(setChains)
      .catch(() => setChains([])); // catálogo opcional: sin él se usa el default
  }, []);

  // El mismo `.catch()` silencioso: si el estado del vínculo no carga, el panel
  // sigue sirviendo para todo lo demás, que es a lo que vino la mayoría.
  useEffect(() => {
    fetchAlexaStatus()
      .then(setAlexa)
      .catch(() => setAlexa(null));
  }, []);

  const desvincularAlexa = async () => {
    try {
      await unlinkAlexa();
      setAlexa({ ...alexa, linked: false, linked_at: null });
    } catch {
      // Sin cartel de error: el botón sigue diciendo "Desvincular" y el usuario
      // puede volver a intentar. Un fallo acá no deja nada a medias.
    }
  };

  const mensajeAlexa = alexaOutcome ? ALEXA_MENSAJES[alexaOutcome] : null;

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

          {/* Alexa va con «Mis datos» por lo mismo que las otras dos: no mueve
              un precio.

              Acá no hay botón de "Vincular", y no es un olvido: el vínculo
              empieza en la app de Alexa, no en esta. El usuario activa el skill
              allá y aprieta "Vincular cuenta"; recién ahí Alexa abre nuestro
              formulario de login. No hay nada que podamos iniciar desde este
              lado, así que la pantalla explica el camino en vez de ofrecer un
              botón que no existe. */}
          <div className="settings-alexa">
            {mensajeAlexa ? (
              <p
                className={`settings-alexa-flash settings-alexa-flash--${mensajeAlexa.tono}`}
                role="status"
              >
                {mensajeAlexa.texto}
              </p>
            ) : null}

            {alexa?.linked ? (
              <>
                <p className="settings-alexa-who">
                  Alexa vinculada
                  {alexa.linked_at
                    ? ` desde el ${new Date(alexa.linked_at).toLocaleDateString('es-AR')}`
                    : ''}
                  {alexa.devices > 1 ? ` (${alexa.devices} cuentas)` : ''}.
                </p>
                <p className="field-hint">
                  Probá diciendo: «Alexa, decile a Ahorrito que agregue leche».
                </p>
                <button
                  className="btn btn--ghost btn--block"
                  onClick={desvincularAlexa}
                >
                  <Mic size={16} strokeWidth={1.75} aria-hidden />
                  Desvincular cuenta de Alexa
                </button>
              </>
            ) : (
              <>
                <p className="settings-alexa-who">
                  <Mic size={16} strokeWidth={1.75} aria-hidden /> Alexa
                </p>
                <p className="field-hint">
                  {alexa && !alexa.configured
                    ? 'No está configurado en este servidor.'
                    : 'Buscá «Ahorrito» en la app de Alexa, activá la skill y tocá «Vincular cuenta». Vas a entrar con este mismo email y contraseña. Después podés decir: «Alexa, decile a Ahorrito que agregue leche».'}
                </p>
              </>
            )}
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
