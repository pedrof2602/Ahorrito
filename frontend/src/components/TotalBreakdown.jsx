import { formatCents, formatCentsPlain } from '../lib/money';
import './TotalBreakdown.css';

/**
 * De la suma de la góndola al total que pagás.
 *
 * Sin esto la pantalla se contradice: la columna de precios suma $36.915 y el
 * veredicto dice $26.124, y no hay forma de saber cuál de los dos creer. Los
 * $10.791 de diferencia son reales y de dos orígenes distintos —lo que la cadena
 * descuenta sola (2x1, segunda unidad al 50%) y lo que descuenta la tarjeta— así
 * que se muestran separados en vez de como un descuento genérico.
 */
export function TotalBreakdown({ chain }) {
  if (!chain || (!chain.ownPromoSaving && !chain.cardSaving)) return null;

  return (
    <section className="breakdown">
      <p className="eyebrow">Cómo se llega al total de {chain.name}</p>

      <dl className="breakdown-rows">
        <div className="breakdown-row">
          <dt>Suma de precios de góndola</dt>
          <dd className="num">{formatCentsPlain(chain.baseTotal)}</dd>
        </div>

        {chain.ownPromoSaving > 0 ? (
          <div className="breakdown-row">
            <dt>Promociones de {chain.name}</dt>
            <dd className="num breakdown-off">
              −{formatCentsPlain(chain.ownPromoSaving)}
            </dd>
          </div>
        ) : null}

        {chain.cardSaving > 0 ? (
          <div className="breakdown-row">
            <dt>{chain.instrumentLabel}</dt>
            <dd className="num breakdown-off">
              −{formatCentsPlain(chain.cardSaving)}
            </dd>
          </div>
        ) : null}

        <div className="breakdown-row breakdown-row--total">
          <dt>Total</dt>
          <dd className="num">{formatCents(chain.total)}</dd>
        </div>
      </dl>

      <p className="breakdown-note">
        {chain.simulated
          ? 'Las promociones salen de simular la compra en el checkout de la cadena, no de aplicarle un porcentaje al total: no se acumulan entre sí y el motor elige una por producto.'
          : 'Esta cadena no tiene checkout consultable, así que su descuento no está medido sino calculado.'}
      </p>
    </section>
  );
}
