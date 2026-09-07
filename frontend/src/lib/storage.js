/**
 * Caché local.
 *
 * La lista y los ajustes viven en el backend; acá queda una copia para que el
 * primer render tenga con qué pintar antes de que conteste la API. Sin eso, el
 * CP arrancaría vacío y se llenaría un instante después, y como el CP dispara la
 * comparación eso serían dos consultas a los supermercados en cada arranque en
 * vez de una.
 *
 * De paso es lo que mantiene la app usable con el backend apagado: se lee lo que
 * hay guardado, y lo que se toque se sincroniza cuando vuelva.
 *
 * Todo acceso va envuelto en try/catch: en navegación privada, con las cookies
 * de sitio bloqueadas o con el almacenamiento lleno, `localStorage` **tira** en
 * vez de devolver vacío, y una lista de compras no es motivo para romper la app.
 */

const PREFIX = 'compras';
const VERSION = 'v1';

export const KEYS = {
  list: `${PREFIX}.list.${VERSION}`,
  settings: `${PREFIX}.settings.${VERSION}`,
  result: `${PREFIX}.result.${VERSION}`,
  migrated: `${PREFIX}.migrated.${VERSION}`,
};

/** Qué se subió ya al backend. Una marca por concepto, no una sola global: los
 *  ajustes y la lista se sincronizan en paralelo, y con una marca compartida el
 *  que terminara primero le haría creer al otro que ya le tocó. */
export const MIGRATION = {
  settings: 'settings',
  list: 'list',
};

export function read(key, fallback = null) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function write(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

export function remove(key) {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Nada que hacer: el estado en memoria sigue siendo el bueno.
  }
}

export const DEFAULT_SETTINGS = {
  postalCode: '',
  channel: 'in_store',
  salesChannel: null,
  theme: 'system',
  fullName: '',
  dni: '',
  email: '',
  phone: '',
};

/**
 * Si este navegador ya subió lo suyo al backend.
 *
 * Sin esta marca, vaciar la lista desde el celular y después abrir la compu
 * haría que la compu la vuelva a subir: encontraría el backend vacío y creería
 * que es la primera sincronización. La migración es de una sola vez por
 * navegador, y de ahí en más el backend manda.
 */
export const wasMigrated = (scope) => read(KEYS.migrated, {})?.[scope] === true;

export const markMigrated = (scope) =>
  write(KEYS.migrated, { ...(read(KEYS.migrated, {}) ?? {}), [scope]: true });

/**
 * Los settings se leen con merge sobre el default para que agregar un campo en
 * una versión futura no deje `undefined` en el estado de quien ya venía usando
 * la app: sin esto, el primer render después de un deploy lee un canal que no
 * existe y la comparación sale con parámetros incompletos.
 */
export function readSettings() {
  return { ...DEFAULT_SETTINGS, ...(read(KEYS.settings) ?? {}) };
}
