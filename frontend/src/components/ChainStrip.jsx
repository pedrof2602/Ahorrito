import { formatCents, formatAgeSeconds } from '../lib/money';
import { chainColor, SCOPE_NOTE } from '../lib/model';
import './ChainStrip.css';

/**
 * Los totales de todas las cadenas, uno al lado del otro.
 *
 * Es el segundo gesto de lectura después del veredicto: cuánto sale en cada
 * lado y por qué esa diferencia. Cada tarjeta carga su propia advertencia
 * —cuántos productos le faltan, si el precio es nacional— porque un total sin
 * su cobertura al lado es la forma más fácil de sacar la conclusión equivocada.
 *
 * **Entran todas en pantalla, siempre.** Es una grilla que reparte el ancho que
 * haya, no una fila que scrollea: una comparación de la que se ven dos de
 * cuatro no es una comparación, y obligar a deslizar para saber si la que falta
 * era más barata es justo el trabajo que la pantalla tiene que ahorrar.
 */
export function ChainStrip({ view }) {
  const { chains, failedChains, winner } = view;
  if (!chains.length && !failedChains.length) return null;

  return (
    <section
      className="wallet"
      // En escritorio son exactamente tantas columnas como cadenas, todas del
      // mismo ancho; en teléfono la grilla se arma sola y esto no se usa.
      style={{ '--cols': chains.length + failedChains.length }}
      aria-label="Totales por cadena"
    >
      {chains.map((chain) => {
        const delta = winner ? chain.total - winner.total : 0;
        return (
          <div
            className={`wallet-card${chain.isCheapest ? ' wallet-card--win' : ''}`}
            key={chain.slug}
          >
            <p className="wallet-name">
              <span
                className="wallet-dot"
                style={{ '--dot': chainColor(chain.slug) }}
                aria-hidden
              />
              {chain.name}
              {chain.isCheapest ? (
                <span className="wallet-badge">Más barato</span>
              ) : null}
            </p>
            <p
              className={`wallet-total num display${
                chain.isCheapest ? ' wallet-total--win' : ''
              }`}
            >
              {formatCents(chain.total)}
            </p>

            {/* El precio tachado solo aparece si la tarjeta lo bajó: mostrar un
                "antes" que nunca existió es publicidad, no información. */}
            {chain.cardSaving > 0 ? (
              <p className="wallet-was num">de {formatCents(chain.referenceTotal)}</p>
            ) : null}

            <div className="wallet-meta">
              {!chain.isCheapest && delta > 0 ? (
                <span className="chip num">+{formatCents(delta)}</span>
              ) : null}
              {chain.missing.length ? (
                <span
                  className="chip chip--danger"
                  title={`Sin coincidencia: ${chain.missing.join(', ')}`}
                >
                  {chain.missing.length === 1
                    ? 'falta 1'
                    : `faltan ${chain.missing.length}`}
                </span>
              ) : null}
              {/* La cadena no respondió y este total salió de precios guardados.
                  Va en la tarjeta y no solo en la franja de arriba: el número
                  grande es lo que se compara de un vistazo, y la advertencia
                  tiene que viajar con él. */}
              {chain.stalePrices ? (
                <span
                  className="chip chip--warn"
                  title={
                    'Esta cadena no respondió: es su último precio conocido y ' +
                    'puede estar desactualizado.'
                  }
                >
                  {formatAgeSeconds(chain.freshness?.age_seconds)}
                </span>
              ) : null}
            </div>

            {chain.priceScope && chain.priceScope !== 'store' ? (
              <p className="wallet-scope">{SCOPE_NOTE[chain.priceScope]}</p>
            ) : null}
            {chain.store ? <p className="wallet-scope">{chain.store.name}</p> : null}
          </div>
        );
      })}

      {failedChains.map((chain) => (
        <div className="wallet-card wallet-card--failed" key={chain.slug}>
          <p className="wallet-name">
            <span
              className="wallet-dot"
              style={{ '--dot': chainColor(chain.slug) }}
              aria-hidden
            />
            {chain.name}
          </p>
          <p className="wallet-total num display wallet-total--empty">—</p>
          <div className="wallet-meta">
            <span className="chip chip--danger">sin datos</span>
          </div>
          <p className="wallet-scope">{chain.message}</p>
        </div>
      ))}
    </section>
  );
}
