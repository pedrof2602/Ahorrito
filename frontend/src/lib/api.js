/**
 * Cliente HTTP contra el backend FastAPI.
 *
 * El default es `/api/v1`, que en desarrollo pasa por el proxy de Vite. Se pisa
 * con `VITE_API_URL` para apuntar al backend directo o —el caso real de esta
 * app— a la IP de la máquina en la LAN, porque `localhost` desde el celular es
 * el celular.
 */

const BASE = import.meta.env.VITE_API_URL ?? '/api/v1';

/**
 * Extrae el mensaje de error de una respuesta fallida.
 *
 * El backend escribe sus errores para que los lea una persona («este medio de
 * pago no tiene BIN: no pasa por la red de tarjetas»); reemplazarlos por
 * "HTTP 422" tira a la basura la única explicación útil. Los errores de
 * validación de FastAPI vienen como lista de objetos en vez de string, así que
 * hay que aplanarlos en lugar de renderizar `[object Object]`.
 */
async function errorMessage(response) {
  try {
    const body = await response.json();
    const { detail } = body ?? {};
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      const msgs = detail.map((d) => d?.msg).filter(Boolean);
      if (msgs.length) return msgs.join('. ');
    }
  } catch {
    // Sin cuerpo JSON: nos quedamos con el código.
  }
  return `El backend respondió ${response.status}.`;
}

/**
 * Se avisa cuando el backend contesta 401 con una sesión que creíamos viva.
 *
 * Hace falta porque la sesión puede morir mientras la app está abierta —vence,
 * la cerrás desde otro dispositivo, cambiás la contraseña— y eso no pasa por
 * ninguna llamada de login que el provider pueda observar. Sin esto, la pantalla
 * se llenaría de "El backend respondió 401" en cada panel en vez de volver al
 * formulario.
 */
const unauthorizedHandlers = new Set();

export function onUnauthorized(handler) {
  unauthorizedHandlers.add(handler);
  return () => unauthorizedHandlers.delete(handler);
}

/** Rutas donde un 401 es la respuesta esperada y no "se venció la sesión".
    `/auth/me` al arrancar contesta 401 cuando simplemente no entraste todavía, y
    tratarlo como expiración mostraría un cartel de sesión vencida a quien nunca
    inició una. */
const EXPECTED_401 = ['/auth/me', '/auth/login', '/auth/register'];

async function request(path, { signal, ...options } = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      // La sesión viaja en una cookie httpOnly, y el default de fetch
      // (`same-origin`) no la manda cuando `VITE_API_URL` apunta a otro origen.
      // Con el proxy de Vite daría igual; en el deploy donde el frontend y la
      // API están separados, es la diferencia entre andar y dar 401 siempre.
      credentials: 'include',
      signal,
      ...options,
    });
  } catch (cause) {
    // Un fetch que ni llega a responder es casi siempre el backend apagado, y
    // decirlo ahorra el paseo por la consola del navegador.
    if (cause?.name === 'AbortError') throw cause;
    throw new Error(
      'No se pudo conectar con el backend. ¿Está corriendo en ' +
        `${BASE.startsWith('http') ? BASE : 'localhost:8000'}?`,
      // El error original queda encadenado: el mensaje de arriba es para leer en
      // pantalla, pero en la consola hay que poder ver qué falló de verdad.
      { cause },
    );
  }

  if (response.status === 401 && !EXPECTED_401.some((p) => path.startsWith(p))) {
    unauthorizedHandlers.forEach((handler) => handler());
  }

  if (!response.ok) throw new Error(await errorMessage(response));
  return response.status === 204 ? null : response.json();
}

const json = (body) => ({ method: 'POST', body: JSON.stringify(body) });
const put = (body) => ({ method: 'PUT', body: JSON.stringify(body) });

// --- cuenta ----------------------------------------------------------------
//
// Ninguna de estas funciones devuelve ni guarda un token: la sesión viaja en una
// cookie httpOnly que el JavaScript de esta página no puede leer. Es a propósito
// —un XSS no puede robar lo que no puede leer— y es la razón por la que acá no
// hay nada parecido a `localStorage.setItem('token', ...)`.

export const login = (email, password) =>
  request('/auth/login', json({ email, password }));

export const register = (email, password) =>
  request('/auth/register', json({ email, password }));

export const logout = () => request('/auth/logout', { method: 'POST' });

/** Quién está logueado. Tira si nadie lo está, y eso es una respuesta válida. */
export const fetchMe = () => request('/auth/me');

export const changePassword = (currentPassword, newPassword) =>
  request(
    '/auth/change-password',
    json({ current_password: currentPassword, new_password: newPassword }),
  );

// --- la pantalla principal -------------------------------------------------

/**
 * Totales por cadena con descuento de tarjeta aplicado, y el detalle línea por
 * línea que los respalda (`result.basket`), todo en una sola request.
 */
export const fetchComparison = (payload, signal) =>
  request('/basket/best-payment', { ...json(payload), signal });

// --- fijar el producto de una línea ---------------------------------------

export const searchProducts = (term, { postalCode, signal } = {}) => {
  const params = new URLSearchParams({ q: term, limit: '12', persist: 'false' });
  if (postalCode) params.set('postal_code', postalCode);
  return request(`/search?${params}`, { signal });
};

// --- medios de pago --------------------------------------------------------

export const fetchInstruments = () => request('/payment-instruments');
export const fetchIssuers = () => request('/payment-issuers');

export const createInstrument = (data) =>
  request('/payment-instruments', json(data));

export const deleteInstrument = (id) =>
  request(`/payment-instruments/${id}`, { method: 'DELETE' });

/** Simula una compra con y sin la tarjeta: la diferencia es lo que hace. */
export const verifyInstrument = (id) =>
  request(`/payment-instruments/${id}/verify`, { method: 'POST' });

// --- catálogo --------------------------------------------------------------

export const fetchChains = () => request('/chains');

// --- ubicaciones -----------------------------------------------------------

/**
 * La sucursal más cercana de cada cadena, para el mapa.
 *
 * **No trae precios a propósito.** Los totales salen de la comparación que ya
 * está en pantalla; si vinieran de acá, el mapa y la tabla podrían mostrar
 * números distintos para la misma canasta.
 */
export const fetchStoreLocations = (postalCode) => {
  const params = new URLSearchParams();
  if (postalCode) params.set('postal_code', postalCode);
  return request(`/store-locations${params.size ? `?${params}` : ''}`);
};

/**
 * Las direcciones que coinciden con lo que se escribió.
 *
 * Devuelve **varias** y no una: «Av. Belgrano 950» existe en tres partidos
 * distintos, y elegir por el usuario es lo que pone el marker en otra provincia.
 */
export const geocodeAddress = ({ address, city, province }, signal) => {
  const params = new URLSearchParams({ address });
  if (city) params.set('city', city);
  if (province) params.set('province', province);
  return request(`/geocode?${params}`, { signal });
};

// --- sucursales cargadas a mano --------------------------------------------

export const fetchCustomStores = () => request('/custom-stores');

export const createCustomStore = (data) => request('/custom-stores', json(data));

export const updateCustomStore = (id, data) =>
  request(`/custom-stores/${id}`, { method: 'PATCH', body: JSON.stringify(data) });

export const deleteCustomStore = (id) =>
  request(`/custom-stores/${id}`, { method: 'DELETE' });

// --- perfil ----------------------------------------------------------------

/**
 * El backend habla `snake_case` y React `camelCase`.
 *
 * La traducción vive acá, en el borde HTTP, y no en los hooks: así el resto de
 * la app no tiene que acordarse de cuál de las dos convenciones le tocó.
 */
const profileFromApi = (row) => ({
  postalCode: row.postal_code ?? '',
  channel: row.channel,
  salesChannel: row.sales_channel,
  theme: row.theme,
  fullName: row.full_name ?? '',
  dni: row.dni ?? '',
  email: row.email ?? '',
  phone: row.phone ?? '',
});

/**
 * Los campos del perfil, sin lo que sea solo del navegador.
 *
 * Las cadenas vacías viajan como `null`: un campo que borraste y uno que nunca
 * cargaste son lo mismo, y el backend guarda `null` para los dos.
 */
export const profileToApi = (settings) => ({
  postal_code: settings.postalCode || null,
  channel: settings.channel,
  sales_channel: settings.salesChannel,
  theme: settings.theme,
  full_name: settings.fullName || null,
  dni: settings.dni || null,
  email: settings.email || null,
  phone: settings.phone || null,
});

export const fetchProfile = () => request('/profile').then(profileFromApi);

export const saveProfile = (settings) =>
  request('/profile', put(profileToApi(settings))).then(profileFromApi);

// --- domicilios ------------------------------------------------------------

export const fetchAddresses = () => request('/addresses');

export const createAddress = (data) => request('/addresses', json(data));

export const updateAddress = (id, data) =>
  request(`/addresses/${id}`, { method: 'PATCH', body: JSON.stringify(data) });

export const deleteAddress = (id) =>
  request(`/addresses/${id}`, { method: 'DELETE' });

// --- lista de compras ------------------------------------------------------

const lineFromApi = (line) => ({
  query: line.query,
  quantity: line.quantity,
  ean: line.ean,
  pinnedLabel: line.pinned_label,
});

const lineToApi = (line) => ({
  query: line.query,
  quantity: line.quantity,
  ean: line.ean,
  pinned_label: line.pinnedLabel,
});

const listFromApi = (row) => ({
  id: row.id,
  name: row.name,
  lines: row.lines.map(lineFromApi),
});

/** La lista con la que trabaja la pantalla; el backend la crea si no hay. */
export const fetchDefaultList = () =>
  request('/shopping-lists/default').then(listFromApi);

/** Reemplaza la lista entera: el orden lo manda la UI, que es la que lo sabe. */
export const saveList = (id, lines) =>
  request(`/shopping-lists/${id}`, put({ lines: lines.map(lineToApi) })).then(
    listFromApi,
  );
