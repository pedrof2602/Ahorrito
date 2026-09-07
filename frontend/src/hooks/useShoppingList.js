import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { fetchDefaultList, saveList } from '../lib/api';
import {
  KEYS,
  MIGRATION,
  markMigrated,
  read,
  wasMigrated,
  write,
} from '../lib/storage';

/**
 * La lista de compras.
 *
 * Cada línea es `{ id, query, quantity, ean, pinnedLabel }`, que es la forma que
 * pide `BasketLine` del backend más un campo que no viaja a la comparación:
 * `id`, para las keys de React.
 *
 * Vive en el backend, con `localStorage` de caché, y con el mismo orden que los
 * ajustes: se pinta desde la caché al instante, se reconcilia con el backend al
 * montar, y cada cambio se guarda con retraso para no mandar un PUT por tecla.
 * Ver el comentario de `useSettings` para el porqué.
 */

const SAVE_DELAY_MS = 500;

let counter = 0;
const nextId = () => `l${Date.now().toString(36)}${(counter++).toString(36)}`;

/**
 * Normaliza lo que venga de afuera —la caché o el backend— antes de dejarlo
 * entrar al estado, y le pone un `id` a lo que no lo traiga.
 *
 * Es el borde de confianza de la app: si un `quantity` guardado llega como
 * string o como 0, el POST sale con un body que el backend rechaza entero, y el
 * error aparece lejos de acá disfrazado de "el súper no respondió".
 */
function sanitize(stored) {
  if (!Array.isArray(stored)) return [];
  return stored
    .filter((line) => line && typeof line.query === 'string' && line.query.trim())
    .slice(0, 50) // el backend acepta 50 líneas como máximo
    .map((line) => ({
      id: typeof line.id === 'string' ? line.id : nextId(),
      query: line.query.trim().slice(0, 120),
      quantity: Math.min(Math.max(Math.round(Number(line.quantity) || 1), 1), 99),
      ean: typeof line.ean === 'string' && line.ean ? line.ean : null,
      pinnedLabel: typeof line.pinnedLabel === 'string' ? line.pinnedLabel : null,
    }));
}

function reducer(lines, action) {
  switch (action.type) {
    case 'add': {
      const query = action.query.trim();
      if (!query || lines.length >= 50) return lines;
      // Repetir un producto es querer más cantidad, no una línea nueva.
      const existing = lines.findIndex(
        (l) => l.query.toLowerCase() === query.toLowerCase(),
      );
      if (existing !== -1) {
        return lines.map((l, i) =>
          i === existing ? { ...l, quantity: Math.min(l.quantity + 1, 99) } : l,
        );
      }
      return [
        ...lines,
        { id: nextId(), query, quantity: 1, ean: null, pinnedLabel: null },
      ];
    }
    case 'remove':
      return lines.filter((l) => l.id !== action.id);
    case 'setQuantity':
      return lines.map((l) =>
        l.id === action.id
          ? { ...l, quantity: Math.min(Math.max(action.quantity, 1), 99) }
          : l,
      );
    case 'setQuery':
      return lines.map((l) =>
        l.id === action.id ? { ...l, query: action.query.slice(0, 120) } : l,
      );
    case 'pin':
      // Fijar el producto también reescribe el texto de la línea: dejar
      // «leche» sobre un EAN de La Serenísima 1L haría que la lista describa
      // algo distinto de lo que se está comparando.
      return lines.map((l) =>
        l.id === action.id
          ? { ...l, ean: action.ean, pinnedLabel: action.label, query: action.label }
          : l,
      );
    case 'unpin':
      return lines.map((l) =>
        l.id === action.id ? { ...l, ean: null, pinnedLabel: null } : l,
      );
    case 'clear':
      return [];
    case 'replace':
      // Lo que vino del backend pasa por el mismo saneado que la caché: es otro
      // borde de confianza, y una línea con `quantity` fuera de rango rompería
      // la comparación igual de lejos de acá.
      return sanitize(action.lines);
    default:
      return lines;
  }
}

export function useShoppingList() {
  const [lines, dispatch] = useReducer(reducer, null, () =>
    sanitize(read(KEYS.list, [])),
  );
  const [syncError, setSyncError] = useState(null);

  // Mismo criterio que en `useSettings`: no se escribe antes de haber leído, y
  // no se reenvía lo que el servidor ya tiene.
  const listId = useRef(null);
  const lastSaved = useRef(null);

  useEffect(() => {
    let cancelled = false;
    const local = sanitize(read(KEYS.list, []));

    fetchDefaultList()
      .then((remote) => {
        listId.current = remote.id;
        if (!wasMigrated(MIGRATION.list) && !remote.lines.length && local.length) {
          // Primera vez de este navegador: la lista que tenías acá sube en vez
          // de perderse. Una sola vez — después manda el backend, para que
          // vaciarla en el celular no la resucite la compu.
          return saveList(remote.id, local).then((saved) => {
            markMigrated(MIGRATION.list);
            return saved;
          });
        }
        markMigrated(MIGRATION.list);
        return remote;
      })
      .then((resolved) => {
        if (cancelled) return;
        lastSaved.current = fingerprintOf(resolved.lines);
        dispatch({ type: 'replace', lines: resolved.lines });
      })
      .catch((cause) => {
        if (cancelled) return;
        // Sin backend se sigue con la caché. `listId` queda en null, así que
        // tampoco se intenta guardar hasta que haya una lectura buena.
        setSyncError(cause.message);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    write(KEYS.list, lines);
  }, [lines]);

  useEffect(() => {
    if (listId.current === null) return undefined;
    // La huella ignora el `id` de React, que cambia sin que cambie la lista.
    const payload = fingerprintOf(lines);
    if (payload === lastSaved.current) return undefined;

    const timer = setTimeout(() => {
      saveList(listId.current, lines)
        .then(() => {
          lastSaved.current = payload;
          setSyncError(null);
        })
        .catch((cause) => setSyncError(cause.message));
    }, SAVE_DELAY_MS);

    return () => clearTimeout(timer);
  }, [lines]);

  return {
    lines,
    syncError,
    add: useCallback((query) => dispatch({ type: 'add', query }), []),
    remove: useCallback((id) => dispatch({ type: 'remove', id }), []),
    setQuantity: useCallback(
      (id, quantity) => dispatch({ type: 'setQuantity', id, quantity }),
      [],
    ),
    setQuery: useCallback(
      (id, query) => dispatch({ type: 'setQuery', id, query }),
      [],
    ),
    pin: useCallback(
      (id, ean, label) => dispatch({ type: 'pin', id, ean, label }),
      [],
    ),
    unpin: useCallback((id) => dispatch({ type: 'unpin', id }), []),
    clear: useCallback(() => dispatch({ type: 'clear' }), []),
  };
}

/**
 * Huella de lo que se guarda, para no reenviar una lista que no cambió.
 *
 * No es `listFingerprint`: aquella ordena y omite `pinnedLabel` porque le
 * interesa saber si hay que volver a consultar precios, y ni reordenar la lista
 * ni renombrar una línea fijada cambian los precios. Acá sí importan las dos
 * cosas, porque son parte de lo que hay que persistir.
 */
function fingerprintOf(lines) {
  return lines
    .map((l) => `${l.query}|${l.quantity}|${l.ean ?? ''}|${l.pinnedLabel ?? ''}`)
    .join('~');
}

/** Las líneas tal como las pide el backend: sin los campos que son solo de UI. */
export function toBasketLines(lines) {
  return lines.map(({ query, quantity, ean }) =>
    ean ? { query, quantity, ean } : { query, quantity },
  );
}

/**
 * Huella de la lista para detectar cambios reales.
 *
 * Se compara contra la huella de la última comparación traída: reordenar el
 * array o editar una línea y dejarla igual no justifica volver a pegarle a las
 * APIs de los supermercados.
 */
export function listFingerprint(lines) {
  return lines
    .map((l) => `${l.query.toLowerCase()}|${l.quantity}|${l.ean ?? ''}`)
    .sort()
    .join('~');
}
