import { formatCents, formatCentsPlain } from '../lib/money';
import { CONFIDENCE_HINT, SCOPE_NOTE } from '../lib/model';
import { ConfidenceTag } from './ConfidenceTag';

/**
 * Una línea de la lista, con su precio en cada cadena.
 *
 * Colapsada muestra lo justo para escanear una columna de números; expandida
 * muestra *qué producto* trajo cada cadena, que es la pregunta que aparece
 * cuando dos precios difieren demasiado como para ser el mismo producto.
 *
 * Cada precio viaja con el nombre corto de su cadena al lado. En pantalla
 * ancha ese rótulo se esconde porque lo dice la cabecera de la columna; en un
 * teléfono, donde cuatro columnas de precios no entran sin volverse ilegibles,
 * los precios se acomodan en grilla debajo del producto y el rótulo es lo
 * único que dice de quién es cada número.
 */
export function LineRow({ line, chains, expanded, onToggle }) {
  const detailId = `line-${line.id}`;

  return (
    <>
      <button
        className={`line${expanded ? ' line--open' : ''}`}
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span className="line-main">
          <span className="line-name">
            {line.query}
            {line.quantity > 1 ? (
              <span className="line-qty num"> ×{line.quantity}</span>
            ) : null}
          </span>
          <ConfidenceTag confidence={line.confidence} />
        </span>

        {/* `display: contents` en ancho grande: las celdas vuelven a ser hijas
            directas del grid de la fila y quedan alineadas con la cabecera. */}
        <span className="line-prices">
          {chains.map((chain) => {
            const match = line.byChain.get(chain.slug);
            const isCheapest =
              match && line.cheapestChain === chain.slug && chains.length > 1;
            return (
              <span
                key={chain.slug}
                className={`line-price${isCheapest ? ' line-price--win' : ''}${
                  match ? '' : ' line-price--missing'
                }`}
              >
                <span className="line-price-chain">{chain.abbr}</span>
                <span className="line-price-value num">
                  {match ? formatCentsPlain(match.line_total_cents) : '—'}
                </span>
              </span>
            );
          })}
        </span>
      </button>

      {expanded ? (
        <div className="line-detail" id={detailId}>
          <p className="line-detail-hint">{CONFIDENCE_HINT[line.confidence]}</p>
          {line.ean ? <p className="line-detail-ean num">EAN {line.ean}</p> : null}

          <div className="line-detail-chains">
            {chains.map((chain) => {
              const match = line.byChain.get(chain.slug);
              return (
                <div className="line-detail-chain" key={chain.slug}>
                  <p className="line-detail-chain-name">{chain.name}</p>
                  {match ? (
                    <>
                      <p className="line-detail-product">
                        {match.product.brand ? (
                          <span className="line-detail-brand">
                            {match.product.brand}
                          </span>
                        ) : null}
                        {match.product.name}
                      </p>
                      <p className="line-detail-facts">
                        <span className="num">
                          {formatCents(match.offer.price_cents)}
                        </span>
                        {line.quantity > 1 ? (
                          <>
                            {' '}× {line.quantity} ={' '}
                            <span className="num">
                              {formatCents(match.line_total_cents)}
                            </span>
                          </>
                        ) : null}
                        {match.offer.price_per_unit_cents ? (
                          <>
                            {' · '}
                            <span className="num">
                              {formatCents(match.offer.price_per_unit_cents)}
                            </span>
                            /{match.product.measurement_unit ?? 'un'}
                          </>
                        ) : null}
                      </p>
                      <p className="line-detail-scope">
                        {SCOPE_NOTE[match.offer.price_scope]}
                        {match.offer.available ? '' : ' · sin stock'}
                      </p>
                    </>
                  ) : (
                    <p className="line-detail-none">
                      No encontró nada para «{line.query}».
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </>
  );
}
