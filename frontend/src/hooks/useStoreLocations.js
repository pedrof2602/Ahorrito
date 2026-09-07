import { useCallback, useEffect, useState } from 'react';
import { fetchStoreLocations } from '../lib/api';

/**
 * Dónde queda cada cadena.
 *
 * Se carga al abrir el mapa y no al arrancar la app, igual que las tarjetas y
 * los domicilios: la pantalla principal no necesita ubicaciones, y el mapa puede
 * no abrirse nunca.
 *
 * Son datos casi estáticos —las sucursales no se mudan entre dos comparaciones—
 * así que se piden una sola vez por código postal y quedan cacheadas mientras la
 * pestaña siga abierta. Volver a la tabla y regresar no dispara nada.
 */
export function useStoreLocations(active, postalCode) {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  // El CP de lo que hay cargado: si cambia, lo cacheado dejó de servir.
  const [loadedFor, setLoadedFor] = useState(null);

  const load = useCallback(async (code) => {
    setStatus('loading');
    setError(null);
    try {
      const result = await fetchStoreLocations(code);
      setData(result);
      setLoadedFor(code ?? '');
      setStatus('success');
    } catch (cause) {
      setError(cause.message);
      setStatus('error');
    }
  }, []);

  useEffect(() => {
    // Misma situación que en `useInstruments`: la regla apunta a estado derivado
    // calculado en un efecto, y esto es lo otro que los efectos sí son,
    // sincronizar con un sistema externo.
    if (active && loadedFor !== (postalCode ?? '') && status !== 'loading') {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      load(postalCode);
    }
  }, [active, postalCode, loadedFor, status, load]);

  return {
    center:
      data?.center_latitude != null && data?.center_longitude != null
        ? [data.center_latitude, data.center_longitude]
        : null,
    // Desde dónde se midieron las distancias. No es un detalle interno: «la más
    // cercana» significa cosas muy distintas si el centro es tu puerta o el
    // promedio de las sucursales de tu código postal, y la pantalla lo aclara.
    centerSource: data?.center_source ?? null,
    centerLabel: data?.center_label ?? null,
    stores: data?.stores ?? [],
    note: data?.note ?? null,
    status,
    error,
    reload: useCallback(() => load(postalCode), [load, postalCode]),
  };
}
