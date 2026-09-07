import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fetchComparison } from '../lib/api';
import { KEYS, read, write } from '../lib/storage';
import { listFingerprint, toBasketLines } from './useShoppingList';
import { settingsFingerprint } from './useSettings';

/**
 * La comparación: una request, todos los números de la pantalla principal.
 *
 * Dos decisiones que definen el hook:
 *
 * **El fetch nunca es automático al escribir.** Aunque el backend cachea las
 * consultas a las cadenas, la comparación sigue simulando checkouts y cruzando
 * medios de pago, así que tarda y tiene costo del otro lado. Debouncear no
 * alcanzaría. Se dispara al montar, al cerrar el editor con cambios, y cuando
 * lo pedís. El resto del tiempo la pantalla muestra el último resultado bueno,
 * marcado si quedó viejo.
 *
 * **Hay dos cachés y no son la misma.** Esta guarda el último *resultado* en
 * `localStorage` para que la app abra con los números puestos; la del backend
 * guarda los *precios* de cada cadena por seis horas. Por eso `fetchedAt` —
 * cuándo pedimos la comparación— dejó de contestar «de cuándo son estos
 * precios»: eso ahora lo dice el `freshness` que viene en la respuesta, y es lo
 * que la pantalla muestra.
 *
 * **`data` sobrevive al error.** Quedarse sin red mientras leés los precios en
 * la góndola no puede borrarte los precios de la pantalla — es exactamente el
 * peor momento para hacerlo.
 */

const HYDRATE_EMPTY = {
  status: 'idle',
  data: null,
  error: null,
  fetchedAt: null,
  fingerprint: null,
};

function hydrate() {
  const cached = read(KEYS.result);
  if (!cached?.data || !cached?.fetchedAt) return HYDRATE_EMPTY;
  return {
    status: 'success',
    data: cached.data,
    error: null,
    fetchedAt: cached.fetchedAt,
    fingerprint: cached.fingerprint ?? null,
  };
}

/**
 * Un resultado de otro día está vencido aunque la lista no haya cambiado: las
 * promos bancarias dependen del día de la semana —el 35% de los miércoles no
 * existe un jueves— así que mostrar el total de ayer es mostrar un descuento
 * que hoy no te van a hacer.
 */
function fromAnotherDay(timestamp) {
  if (!timestamp) return true;
  return new Date(timestamp).toDateString() !== new Date().toDateString();
}

export function useComparison(lines, settings) {
  const [state, setState] = useState(hydrate);

  const fingerprint = useMemo(
    () => `${listFingerprint(lines)}#${settingsFingerprint(settings)}`,
    [lines, settings],
  );

  // Refs para que `refresh` sea estable: se pasa como callback a varios
  // componentes y no queremos re-renderizarlos cada vez que cambia una cantidad.
  const linesRef = useRef(lines);
  const settingsRef = useRef(settings);
  const fingerprintRef = useRef(fingerprint);
  linesRef.current = lines;
  settingsRef.current = settings;
  fingerprintRef.current = fingerprint;

  const controllerRef = useRef(null);
  const requestedRef = useRef(null);
  // Identifica cada intento. Sin esto, la respuesta de una consulta ya
  // reemplazada puede pisar el estado de la que sí importa: son varios segundos
  // de ventana, más que suficiente para que se crucen.
  const attemptRef = useRef(0);

  const run = useCallback(async (fresh) => {
    const currentLines = linesRef.current;
    if (!currentLines.length) return;

    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    const attempt = ++attemptRef.current;
    const requested = fingerprintRef.current;
    requestedRef.current = requested;
    setState((prev) => ({ ...prev, status: 'loading', error: null }));

    const { postalCode, channel, salesChannel } = settingsRef.current;
    try {
      const data = await fetchComparison(
        {
          lines: toBasketLines(currentLines),
          postal_code: postalCode || null,
          sales_channel: salesChannel ?? null,
          channel,
          fresh,
        },
        controller.signal,
      );
      if (attemptRef.current !== attempt) return; // llegó tarde: ya hay otra
      const fetchedAt = Date.now();
      setState({
        status: 'success',
        data,
        error: null,
        fetchedAt,
        fingerprint: requested,
      });
      write(KEYS.result, { data, fetchedAt, fingerprint: requested });
    } catch (error) {
      // Si ya arrancó otro intento, ese es el dueño del estado y de la huella.
      if (attemptRef.current !== attempt) return;
      // Se libera la huella para que el próximo intento no se crea ya hecho.
      // Vale también para la cancelación: React monta, desmonta y vuelve a
      // montar en desarrollo, y ese desmontaje aborta la consulta inicial. Sin
      // liberar la huella acá, al remontar se cree ya pedida y la app se queda
      // en el esqueleto para siempre.
      requestedRef.current = null;
      if (error?.name === 'AbortError') return;
      setState((prev) => ({ ...prev, status: 'error', error: error.message }));
    }
  }, []);

  /**
   * Vuelve a comparar aprovechando la caché del backend.
   *
   * Es lo correcto cuando lo que cambió es de este lado —la lista, el CP, las
   * tarjetas—: los precios que el backend tenga guardados siguen valiendo, y
   * pedirle que los tire sería hacer 60 requests a los súper para recibir los
   * mismos números.
   *
   * No toma argumentos a propósito: se pasa como `onClick` en varios lados, y
   * con un parámetro recibiría el evento del click como si fuera una opción.
   */
  const refresh = useCallback(() => run(false), [run]);

  /**
   * Ignora la caché del backend y consulta a las cadenas en vivo.
   *
   * Es lo que hace el botón «actualizar precios»: alguien que lo aprieta está
   * pidiendo justamente que no le contesten con lo guardado. Si una cadena no
   * responde igual se sirve su último precio conocido, marcado.
   */
  const refreshLive = useCallback(() => run(true), [run]);

  // Al montar: si lo cacheado no corresponde a esta lista, o es de otro día, se
  // refresca solo. Si corresponde, la app abre con los números puestos y no
  // pide nada — que es el caso normal de abrirla dos veces en la misma tarde.
  useEffect(() => {
    if (!linesRef.current.length) return;
    const cachedIsUsable =
      state.fingerprint === fingerprintRef.current &&
      !fromAnotherDay(state.fetchedAt);
    if (!cachedIsUsable && requestedRef.current !== fingerprintRef.current) {
      refresh();
    }
    // Solo al montar: el resto de los disparos son explícitos.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A propósito no se aborta la consulta al desmontar. Las que hay que cancelar
  // —las que quedaron viejas— ya las cancela `refresh` al empezar la siguiente,
  // que es donde importa. Abortar también al desmontar hacía que el
  // monta-desmonta-monta de React en desarrollo cancelara la consulta inicial y
  // pidiera una segunda: dos fan-outs contra Carrefour y Disco en cada recarga
  // de la página, por nada. Este componente vive lo que vive la pestaña, y
  // cuando se cierra el navegador corta la conexión igual.

  const clear = useCallback(() => {
    controllerRef.current?.abort();
    requestedRef.current = null;
    setState(HYDRATE_EMPTY);
    write(KEYS.result, null);
  }, []);

  return {
    ...state,
    /** Los números en pantalla ya no describen la lista actual. */
    isStale:
      state.data != null &&
      (state.fingerprint !== fingerprint || fromAnotherDay(state.fetchedAt)),
    /** La lista cambió respecto de lo que se comparó (no solo envejeció). */
    listChanged: state.data != null && state.fingerprint !== fingerprint,
    refresh,
    refreshLive,
    clear,
  };
}
