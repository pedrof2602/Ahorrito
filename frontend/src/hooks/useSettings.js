import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchProfile, profileToApi, saveProfile } from '../lib/api';
import {
  KEYS,
  MIGRATION,
  markMigrated,
  readSettings,
  wasMigrated,
  write,
} from '../lib/storage';

/**
 * Código postal, canal, canal de venta, tema y tus datos.
 *
 * No son preferencias cosméticas: el CP resuelve la sucursal en las cadenas que
 * regionalizan, y el canal cambia qué promos bancarias aplican (muchas son solo
 * presenciales o solo online). Cambiar cualquiera de los dos invalida los
 * números en pantalla, por eso viven al lado del veredicto y no en un menú.
 *
 * Viven en el backend, con `localStorage` de caché. El orden importa:
 *
 * 1. El estado arranca de la caché, **sincrónicamente**. Si arrancara vacío y se
 *    llenara al contestar la API, el CP cambiaría de valor después del primer
 *    render y eso dispararía una segunda comparación contra los supermercados.
 * 2. Se reconcilia con el backend una vez, al montar.
 * 3. Cada cambio se escribe en la caché al toque y al backend con retraso, para
 *    no mandar un PUT por tecla mientras se tipea el CP.
 *
 * Si el backend no contesta, la app sigue andando con la caché: el error se
 * guarda para mostrarlo, pero no rompe la pantalla.
 */

const SAVE_DELAY_MS = 500;

export function useSettings() {
  const [settings, setSettings] = useState(readSettings);
  const [syncError, setSyncError] = useState(null);

  // Hasta que el backend no contestó no se le escribe: un PUT antes de la
  // primera lectura pisaría con la caché de este navegador lo que hayas
  // guardado desde otro.
  const synced = useRef(false);
  // Lo último que el servidor confirmó, para no reenviar lo que ya tiene.
  const lastSaved = useRef(null);

  useEffect(() => {
    let cancelled = false;
    const local = readSettings();

    fetchProfile()
      .then((remote) => {
        // Primera vez de este navegador contra un backend sin datos: lo que
        // había acá sube en lugar de perderse. Una sola vez — después manda el
        // backend, para que borrar algo en un dispositivo no lo resucite otro.
        if (!wasMigrated(MIGRATION.settings) && isBlank(remote) && !isBlank(local)) {
          return saveProfile(local).then((saved) => {
            markMigrated(MIGRATION.settings);
            return saved;
          });
        }
        markMigrated(MIGRATION.settings);
        return remote;
      })
      .then((resolved) => {
        if (cancelled) return;
        synced.current = true;
        lastSaved.current = JSON.stringify(profileToApi(resolved));
        setSettings(resolved);
      })
      .catch((cause) => {
        if (cancelled) return;
        // Backend caído: se sigue con la caché. No se marca `synced`, así que
        // tampoco se intenta escribir hasta que haya una lectura buena.
        setSyncError(cause.message);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // La caché se actualiza siempre y al instante: es lo que va a leer el próximo
  // arranque, tenga o no backend.
  useEffect(() => {
    write(KEYS.settings, settings);
  }, [settings]);

  useEffect(() => {
    if (!synced.current) return undefined;
    const payload = JSON.stringify(profileToApi(settings));
    if (payload === lastSaved.current) return undefined;

    const timer = setTimeout(() => {
      saveProfile(settings)
        .then(() => {
          lastSaved.current = payload;
          setSyncError(null);
        })
        .catch((cause) => setSyncError(cause.message));
    }, SAVE_DELAY_MS);

    return () => clearTimeout(timer);
  }, [settings]);

  // El tema se escribe en el `<html>` y no en un contexto de React: así lo ven
  // también los `color-scheme` de los controles nativos (scrollbars, inputs de
  // fecha), que no saben nada de nuestro árbol de componentes.
  useEffect(() => {
    const root = document.documentElement;
    if (settings.theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', settings.theme);
  }, [settings.theme]);

  const update = useCallback((patch) => {
    setSettings((prev) => ({ ...prev, ...patch }));
  }, []);

  return { settings, update, syncError };
}

/** Un perfil sin nada cargado: es el que devuelve una instalación nueva. */
function isBlank(settings) {
  return !(
    settings.postalCode ||
    settings.fullName ||
    settings.dni ||
    settings.email ||
    settings.phone
  );
}

/** Huella de lo que, al cambiar, obliga a recomparar. El tema no entra. */
export function settingsFingerprint({ postalCode, channel, salesChannel }) {
  return `${postalCode ?? ''}|${channel}|${salesChannel ?? ''}`;
}

export const CHANNEL_LABEL = {
  in_store: 'presencial',
  online: 'online',
};
