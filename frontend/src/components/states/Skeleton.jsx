import { Loader2 } from 'lucide-react';
import './states.css';

/**
 * La espera.
 *
 * Esto no consulta una caché: hace fan-out a las APIs de Carrefour y Disco y
 * simula sus checkouts, así que son segundos. Un spinner suelto durante ese
 * rato no dice nada; el esqueleto tiene la **forma real** del resultado y tantas
 * filas como productos hay en la lista, así que la página no salta cuando
 * llegan los datos y ya se ve dónde va a estar cada número.
 */
export function Skeleton({ rows = 6 }) {
  return (
    <div className="skeleton" aria-busy="true" aria-live="polite">
      <p className="visually-hidden">Consultando precios…</p>

      <div className="skeleton-verdict">
        <div className="sk sk-pulse" style={{ width: 96, height: 12 }} />
        <div
          className="sk sk-pulse"
          style={{ width: 168, height: 26, marginTop: 12 }}
        />
        <div
          className="sk sk-pulse"
          style={{ width: 210, height: 40, marginTop: 8 }}
        />
      </div>

      <div className="skeleton-strip">
        {[0, 1].map((i) => (
          <div key={i}>
            <div className="sk sk-pulse" style={{ width: 64, height: 11 }} />
            <div
              className="sk sk-pulse"
              style={{ width: 92, height: 20, marginTop: 8 }}
            />
          </div>
        ))}
      </div>

      <div className="skeleton-status">
        <Loader2 size={14} className="spin" aria-hidden />
        Consultando Carrefour y Disco…
      </div>

      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton-row" key={i}>
          <div
            className="sk sk-pulse"
            style={{
              // Anchos irregulares: una columna de barras idénticas se lee como
              // un patrón, no como una lista de productos distintos.
              width: `${58 + ((i * 37) % 34)}%`,
              height: 13,
              animationDelay: `${i * 90}ms`,
            }}
          />
          <div
            className="sk sk-pulse"
            style={{ height: 13, animationDelay: `${i * 90}ms` }}
          />
          <div
            className="sk sk-pulse"
            style={{ height: 13, animationDelay: `${i * 90}ms` }}
          />
        </div>
      ))}
    </div>
  );
}
