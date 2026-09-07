import { useCallback, useEffect, useState } from 'react';
import {
  createInstrument,
  deleteInstrument,
  fetchInstruments,
  fetchIssuers,
  verifyInstrument,
} from '../lib/api';

/**
 * Mis tarjetas y billeteras.
 *
 * Se cargan cuando se abre el panel, no al arrancar la app: la pantalla
 * principal no las necesita —los descuentos ya vienen resueltos dentro de la
 * comparación— y una request menos al abrir es tiempo que se gana.
 *
 * `verify` merece su propio estado de carga porque es lento de verdad: simula
 * una compra chica con y sin la tarjeta contra el checkout real de la cadena, y
 * la diferencia entre las dos simulaciones es, literalmente, lo que hace esa
 * tarjeta.
 */
export function useInstruments(active) {
  const [instruments, setInstruments] = useState([]);
  const [issuers, setIssuers] = useState([]);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [verification, setVerification] = useState(null);

  const load = useCallback(async () => {
    setStatus('loading');
    setError(null);
    try {
      const [list, catalog] = await Promise.all([
        fetchInstruments(),
        // Los emisores son un catálogo estático; si falla, se puede seguir
        // cargando una tarjeta a mano, así que no tumba la pantalla.
        fetchIssuers().catch(() => []),
      ]);
      setInstruments(list);
      setIssuers(catalog);
      setStatus('success');
    } catch (cause) {
      setError(cause.message);
      setStatus('error');
    }
  }, []);

  useEffect(() => {
    // La regla apunta a estado derivado calculado en un efecto; esto es lo otro
    // que los efectos sí son, sincronizar con un sistema externo. Sin una
    // librería de fetching —que se descartó a propósito por una sola query— no
    // hay forma de pedir datos al montar que no dispare esta regla.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (active && status === 'idle') load();
  }, [active, status, load]);

  const create = useCallback(async (payload) => {
    setError(null);
    const created = await createInstrument(payload);
    setInstruments((prev) => [...prev, created]);
    return created;
  }, []);

  const remove = useCallback(async (id) => {
    setError(null);
    setBusyId(id);
    try {
      await deleteInstrument(id);
      setInstruments((prev) => prev.filter((i) => i.id !== id));
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusyId(null);
    }
  }, []);

  const verify = useCallback(async (id) => {
    setError(null);
    setVerification(null);
    setBusyId(id);
    try {
      const result = await verifyInstrument(id);
      setVerification({ id, result });
      // El backend sella `verified_at` y las promos encontradas del lado suyo;
      // se relee para no adivinar acá qué guardó.
      setInstruments(await fetchInstruments());
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusyId(null);
    }
  }, []);

  return {
    instruments,
    issuers,
    status,
    error,
    busyId,
    verification,
    create,
    remove,
    verify,
    reload: load,
  };
}
