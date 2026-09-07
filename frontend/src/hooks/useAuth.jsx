import { useCallback, useEffect, useMemo, useState } from 'react';
import * as api from '../lib/api';
import { AuthContext } from '../lib/authContext';

/**
 * Quién está logueado, para toda la app.
 *
 * El estado tiene tres valores y no dos, y esa es la parte que importa:
 * `checking` existe porque al arrancar todavía no sabemos si hay sesión —hay que
 * preguntarle al backend, porque la cookie es httpOnly y desde acá no se puede
 * mirar—. Sin ese tercer estado, el primer render diría "no estás logueado" y
 * mostraría el formulario de login por un instante a alguien que sí tenía sesión.
 * Ese parpadeo es de los que se notan en cada recarga.
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [status, setStatus] = useState('checking');
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    let cancelled = false;

    api
      .fetchMe()
      .then((me) => {
        if (cancelled) return;
        setUser(me);
        setStatus('authenticated');
      })
      .catch(() => {
        // Acá no se distingue "no hay sesión" de "el backend está apagado", y
        // está bien: en los dos casos lo único que la app puede ofrecer es el
        // formulario, que además es donde el error de conexión se va a ver.
        if (cancelled) return;
        setUser(null);
        setStatus('anonymous');
      });

    return () => {
      cancelled = true;
    };
  }, []);

  /* La sesión puede morir con la app abierta: vence, la cerrás desde otro
     dispositivo, o cambiás la contraseña. Eso no pasa por ninguna función de
     este archivo, así que se escucha el 401 desde el cliente HTTP. */
  useEffect(
    () =>
      api.onUnauthorized(() => {
        setUser(null);
        setStatus('anonymous');
        setExpired(true);
      }),
    [],
  );

  const authenticate = useCallback(async (fn, email, password) => {
    const me = await fn(email, password);
    setUser(me);
    setStatus('authenticated');
    setExpired(false);
    return me;
  }, []);

  const value = useMemo(
    () => ({
      user,
      status,
      expired,
      login: (email, password) => authenticate(api.login, email, password),
      register: (email, password) => authenticate(api.register, email, password),
      logout: async () => {
        /* El estado local se limpia pase lo que pase con la request. Si el
           backend no contesta y dejáramos la sesión "abierta" en pantalla, quien
           quiso salir se queda adentro, que es lo contrario de lo que pidió. */
        try {
          await api.logout();
        } finally {
          setUser(null);
          setStatus('anonymous');
          setExpired(false);
        }
      },
    }),
    [user, status, expired, authenticate],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}