import { useCallback, useEffect, useState } from 'react';
import {
  createCustomStore,
  deleteCustomStore,
  fetchCustomStores,
  updateCustomStore,
} from '../lib/api';

/**
 * Las sucursales que cargó el usuario a mano.
 *
 * Se cargan al abrir el panel, igual que los domicilios y las tarjetas: la
 * pantalla principal no las necesita porque el mapa ya las recibe mezcladas con
 * las automáticas desde `/store-locations`.
 */
export function useCustomStores(active) {
  const [stores, setStores] = useState([]);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    setStatus('loading');
    setError(null);
    try {
      setStores(await fetchCustomStores());
      setStatus('success');
    } catch (cause) {
      setError(cause.message);
      setStatus('error');
    }
  }, []);

  useEffect(() => {
    // Misma situación que en `useAddresses`: la regla apunta a estado derivado
    // calculado en un efecto, y esto es lo otro que los efectos sí son,
    // sincronizar con un sistema externo.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (active && status === 'idle') load();
  }, [active, status, load]);

  const create = useCallback(async (payload) => {
    setError(null);
    const created = await createCustomStore(payload);
    setStores((prev) =>
      [...prev, created].sort((a, b) => a.name.localeCompare(b.name, 'es')),
    );
    return created;
  }, []);

  const update = useCallback(async (id, patch) => {
    setError(null);
    setBusyId(id);
    try {
      const saved = await updateCustomStore(id, patch);
      setStores((prev) => prev.map((s) => (s.id === id ? saved : s)));
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusyId(null);
    }
  }, []);

  const remove = useCallback(async (id) => {
    setError(null);
    setBusyId(id);
    try {
      await deleteCustomStore(id);
      setStores((prev) => prev.filter((s) => s.id !== id));
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusyId(null);
    }
  }, []);

  return { stores, status, error, busyId, create, update, remove, reload: load };
}
