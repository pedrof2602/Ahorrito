import { createContext, useContext } from 'react';

/**
 * El contexto de sesión, separado del provider.
 *
 * Viven aparte por la regla de fast-refresh: un archivo que exporta un
 * componente y además otra cosa pierde el hot-reload de ese componente. El
 * provider —que es lo que se edita seguido mientras se trabaja en la app— queda
 * solo en `hooks/useAuth.jsx`, y acá quedan las dos piezas que casi nunca cambian.
 */
export const AuthContext = createContext(null);

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error('useAuth necesita estar dentro de <AuthProvider>.');
  }
  return context;
}
