/**
 * Formateo de plata.
 *
 * El backend manda todo en centavos enteros para no acumular drift de punto
 * flotante en el histórico, y acá se convierte una sola vez, al mostrar.
 *
 * **Los centavos no se muestran nunca.** En una compra de $80.000 son ruido, y
 * mostrarlos en un número y no en otro es peor que no mostrarlos: rompe la
 * alineación de las columnas, que es justamente lo que hace rápida la lectura.
 */

const ARS = new Intl.NumberFormat('es-AR', {
  style: 'currency',
  currency: 'ARS',
  maximumFractionDigits: 0,
});

const PLAIN = new Intl.NumberFormat('es-AR', { maximumFractionDigits: 0 });

/**
 * `$84.320`. Para totales y cualquier número que necesite el símbolo.
 *
 * `Intl` separa el `$` de la cifra con un espacio duro, y acá se saca entero.
 * No es una preferencia tipográfica: a 44px ese hueco parte el número en dos, y
 * en la monoespaciada de escritorio es peor todavía, porque ahí *todos* los
 * espacios miden una celda entera —incluido el fino— y el símbolo termina
 * flotando lejos de su cifra. Pegado se lee como un solo número, que es lo que
 * es.
 */
export function formatCents(cents) {
  if (cents == null || Number.isNaN(cents)) return '—';
  return ARS.format(Math.round(cents / 100)).replace(/[\s\u00a0\u202f]/g, '');
}

/**
 * `84.320`, sin símbolo. Es lo que va en las columnas de la tabla: repetir el
 * `$` en cada celda de una grilla de precios agrega ruido sin agregar
 * información, y desalinea los dígitos que se quieren comparar.
 */
export function formatCentsPlain(cents) {
  if (cents == null || Number.isNaN(cents)) return '—';
  return PLAIN.format(Math.round(cents / 100));
}

/** `12%` o `12,5%`, sin decimal inútil. */
export function formatPercent(percent) {
  if (percent == null || Number.isNaN(percent)) return '';
  const rounded = Math.round(percent * 10) / 10;
  return `${rounded.toLocaleString('es-AR')}%`;
}

/**
 * Cuánto hace, a partir de una antigüedad en segundos.
 *
 * Es la forma que usa el backend: cada precio viaja con su `age_seconds`, que es
 * hace cuánto se le preguntó **a la cadena**. No se deriva de un timestamp
 * porque los dos relojes no son el mismo: el del navegador puede estar corrido, y
 * un desfase de minutos convertiría "recién" en "hace 20 min" o al revés.
 */
export function formatAgeSeconds(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return null;
  const safe = Math.max(0, seconds);
  if (safe < 90) return 'recién';
  const minutes = Math.round(safe / 60);
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'ayer' : `hace ${days} días`;
}

/** Cuánto hace que se trajeron estos números. Es un dato, no un adorno: un
 *  total de hace tres días puede mandarte al súper equivocado. */
export function formatAge(timestamp) {
  if (!timestamp) return null;
  return formatAgeSeconds((Date.now() - timestamp) / 1000);
}
