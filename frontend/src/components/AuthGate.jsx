import { useAuth } from '../lib/authContext';
import { LoginScreen } from './LoginScreen';

/**
 * Decide entre la app y el login, y no monta la app hasta saber cuál va.
 *
 * Lo importante es lo que hace durante `checking`: no renderiza nada. Si
 * dejara pasar a la app mientras averigua si hay sesión, todos los hooks que
 * cargan datos al montar —lista, perfil, tarjetas, comparación— dispararían sus
 * requests, cobrarían 401 y pintarían la pantalla de errores un instante antes
 * de que apareciera el formulario.
 *
 * Y cuando la sesión se cierra, la app se desmonta de verdad. Es lo que
 * garantiza que al entrar otra cuenta no queden en memoria la lista ni los
 * totales de la anterior: no hay que acordarse de limpiar cada hook porque
 * ninguno sobrevive al cambio.
 */
export function AuthGate({ children }) {
  const { status } = useAuth();

  if (status === 'checking') {
    /* Deliberadamente vacío y no un spinner: la respuesta llega en decenas de
       milisegundos contra un backend local, y un spinner que aparece y se va en
       ese tiempo se lee como un parpadeo, no como carga. */
    return null;
  }

  return status === 'authenticated' ? children : <LoginScreen />;
}
