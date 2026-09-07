import { useCallback, useEffect, useState } from 'react';
import {
  createAddress,
  deleteAddress,
  fetchAddresses,
  updateAddress,
} from '../lib/api';

/**
 * Mis domicilios.
 *
 * Se cargan cuando se abre el panel y no al arrancar la app, igual que las
 * tarjetas: la pantalla principal no los necesita —el CP que resuelve la
 * sucursal está en los ajustes, no acá— y una request menos al abrir es tiempo
 * que se gana.
 */
export function useAddresses(active) {
  const [addresses, setAddresses] = useState([]);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    setStatus('loading');
    setError(null);
    try {
      setAddresses(await fetchAddresses());
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
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (active && status === 'idle') load();
  }, [active, status, load]);

  const create = useCallback(async (payload) => {
    setError(null);
    const created = await createAddress(payload);
    // Se relee en vez de agregar a mano: marcar uno como predeterminado
    // desmarca los otros del lado del servidor, y esos cambios no están en la
    // respuesta del alta.
    setAddresses(await fetchAddresses());
    return created;
  }, []);

  const update = useCallback(async (id, patch) => {
    setError(null);
    setBusyId(id);
    try {
      await updateAddress(id, patch);
      setAddresses(await fetchAddresses());
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
      await deleteAddress(id);
      setAddresses((prev) => prev.filter((a) => a.id !== id));
    } catch (cause) {
      setError(cause.message);
    } finally {
      setBusyId(null);
    }
  }, []);

  return { addresses, status, error, busyId, create, update, remove, reload: load };
}
