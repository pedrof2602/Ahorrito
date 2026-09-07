import { AlertTriangle, ArrowDown, Clock, CreditCard } from 'lucide-react';
import { formatCents, formatPercent, formatAgeSeconds } from '../lib/money';
import './Verdict.css';

/**
 * La respuesta, en el primer viewport: dónde comprar y cuánto sale.
 *
 * Todo lo demás en la app existe para respaldar estas cuatro líneas. Por eso el
 * bloque no muestra nada que no sirva para decidir, y por eso cuando la
 * comparación no se sostiene —una cadena caída, o la ganadora sin la canasta
 * completa— deja de dar un ganador en vez de dar uno con asterisco.
 */
export function Verdict({ view }) {
  const { winner, runnerUp, savingVsRunnerUp, winnerHasGaps, partial } = view;

  if (!winner) {
    return (
      <section className="verdict-stage">
        <div className="verdict">
          <p className="eyebrow">Sin resultados</p>
          <p className="verdict-blocked">
            Ninguna cadena devolvió precios para esta lista.
          </p>
        </div>
      </section>
    );
  }

  const promo = winner.bankPromo;

  return (
    <section className="verdict-stage">
      <div className="verdict">
        <p className="eyebrow">
          <span className="eyebrow-dot" aria-hidden />
          {partial ? 'Más barato de lo que respondió' : 'Hoy conviene'}
        </p>
        <h1 className="verdict-chain display">{winner.name}</h1>
        <p className="verdict-total num display">{formatCents(winner.total)}</p>

        {savingVsRunnerUp > 0 && runnerUp ? (
          <p className="verdict-saving">
            <ArrowDown size={15} strokeWidth={2.5} aria-hidden />
            <span className="num">{formatCents(savingVsRunnerUp)}</span>
            &nbsp;más barato que {runnerUp.name}
          </p>
        ) : null}

        {savingVsRunnerUp === 0 && runnerUp ? (
          <p className="verdict-tie">Empatan con {runnerUp.name}: da igual dónde.</p>
        ) : null}

        {/* El caso que arruina este tipo de app: la cadena más barata lo es
            porque no tiene todo. Se dice antes de que el número convenza. */}
        {winnerHasGaps ? (
          <p className="verdict-caveat">
            <AlertTriangle size={14} strokeWidth={2} aria-hidden />
            <span>
              A {winner.name} le {winner.missing.length === 1 ? 'falta' : 'faltan'}{' '}
              {winner.missing.length} de tus {view.totalCount} productos: este total
              es más bajo por eso, no necesariamente por ser más barato.
            </span>
          </p>
        ) : null}

        {/* El ganador ganó con precios que su cadena no confirmó hoy. Se dice
            acá y no solo en la franja de arriba porque es lo que sostiene el
            número grande: el resto de las advertencias de esta caja hablan de
            cuánto vale la comparación, y esta también.

            El ahorro se sigue mostrando, a diferencia de `winnerHasGaps`: allá
            el total está mal *por construcción* —le faltan productos—, acá está
            bien pero es de antes. Tacharlo sería tratar «viejo» como
            «inventado», y dejaría sin número a quien igual tiene que decidir. */}
        {winner.stalePrices ? (
          <p className="verdict-caveat verdict-caveat--soft">
            <Clock size={14} strokeWidth={2} aria-hidden />
            <span>
              {winner.name} no respondió: este total sale de sus precios de{' '}
              {formatAgeSeconds(winner.freshness?.age_seconds)} y pueden haber
              cambiado.
            </span>
          </p>
        ) : null}

        {/* Que una cadena se haya caído ya lo dice la franja de arriba, con su
            nombre y el motivo. Acá alcanza con no afirmar un ganador: el rótulo
            pasó a «más barato de lo que respondió» y no se muestra ningún ahorro.
            Repetirlo en una segunda caja ámbar sería ruido, no cautela. */}

        <div className="verdict-card">
          {winner.instrumentLabel ? (
            <>
              <span className="verdict-instrument">
                <CreditCard
                  size={16}
                  strokeWidth={1.75}
                  className="verdict-instrument-icon"
                  aria-hidden
                />
                con <strong>{winner.instrumentLabel}</strong>
                {winner.cardSaving > 0 ? (
                  <>
                    {' '}ahorrás{' '}
                    <span className="num">{formatCents(winner.cardSaving)}</span>
                  </>
                ) : null}
              </span>
              <span className="verdict-tags">
                {winner.confidenceLabel ? (
                  <span
                    className={`chip ${
                      winner.confidence === 'measured' ? '' : 'chip--warn'
                    }`}
                  >
                    {winner.confidenceLabel}
                  </span>
                ) : null}
                {promo?.unverified ? (
                  <span className="chip chip--warn">promo sin verificar</span>
                ) : null}
                {promo?.capped ? <span className="chip chip--warn">con tope</span> : null}
                {winner.isReimbursement ? (
                  <span className="chip chip--warn">reintegro</span>
                ) : null}
              </span>
            </>
          ) : (
            <span className="verdict-nocard">
              Sin tarjetas cargadas: es el precio de góndola.
            </span>
          )}
        </div>

        {promo ? (
          <p className="verdict-promo">
            {promo.name} · {formatPercent(promo.percent)}
            {promo.capped
              ? ` — el tope corta el ahorro en ${formatCents(promo.saving_cents)}`
              : ''}
          </p>
        ) : null}

        {/* Un reintegro ahorra lo mismo, pero no hoy: en la caja pagás todo. */}
        {winner.isReimbursement ? (
          <p className="verdict-promo">
            En la caja pagás{' '}
            <span className="num">{formatCents(winner.outOfPocket)}</span>; el resto
            vuelve después.
          </p>
        ) : null}
      </div>
    </section>
  );
}
