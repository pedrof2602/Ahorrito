import { useState } from 'react';
import { LineRow } from './LineRow';
import './LineTable.css';

/**
 * El detalle producto por producto, como tabla de columnas alineadas.
 *
 * La decisión de forma: **una fila por producto, no una tarjeta por producto.**
 * Comparar precios se hace leyendo hacia abajo por columna, y eso solo funciona
 * si los números están alineados y la cabecera dice qué columna es cuál sin
 * tener que recordarlo — por eso queda fija al scrollear.
 */
export function LineTable({ view }) {
  const [openId, setOpenId] = useState(null);
  const { chains, lines, comparableCount, totalCount } = view;

  if (!lines.length) return null;

  return (
    <section className="table" style={{ '--cols': chains.length }}>
      <div className="table-summary">
        <span className="eyebrow">
          {totalCount} {totalCount === 1 ? 'producto' : 'productos'}
        </span>
        {/* Que son de góndola importa: no incluyen las promos que la cadena
            aplica recién en la caja, así que esta columna no suma el total. */}
        <span className="table-summary-note">
          precios de góndola
          {/* Con una sola cadena en pie no hay «todas las cadenas» que contar:
              el dato sería «0 en todas las cadenas», que no significa nada. */}
          {chains.length > 1 && comparableCount < totalCount
            ? ` · ${comparableCount} en todas`
            : ''}
        </span>
      </div>

      <div className="line-head">
        {/* No usa `.visually-hidden`: esa clase saca el elemento del flujo con
            `position:absolute`, y al hacerlo deja de ocupar su celda en este
            grid — los nombres de cadena de al lado se corren una columna a la
            izquierda y quedan desalineados con los precios de las filas de
            abajo. `.line-head-label` oculta lo mismo sin sacarlo del flujo. */}
        <span className="line-head-label">Producto</span>
        {chains.map((chain) => (
          <span key={chain.slug} className="line-head-chain">
            {chain.abbr}
          </span>
        ))}
      </div>

      <div className="table-rows">
        {lines.map((line) => (
          <LineRow
            key={line.id}
            line={line}
            chains={chains}
            expanded={openId === line.id}
            onToggle={() => setOpenId((id) => (id === line.id ? null : line.id))}
          />
        ))}
      </div>
    </section>
  );
}
