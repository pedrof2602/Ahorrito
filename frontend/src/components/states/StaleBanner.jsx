import { Clock } from 'lucide-react';
import { formatAgeSeconds } from '../../lib/money';
import './states.css';

/**
 * Una cadena no respondió y compite con su último precio conocido.
 *
 * Es el caso que el backend eligió no tratar como error: perder Disco entero
 * porque su API tardó 20 segundos deja al usuario sin la comparación que sí
 * podíamos darle. Pero un precio viejo que se muestra sin decirlo es peor que no
 * mostrarlo, así que la contrapartida de servirlo es esta franja.
 *
 * Franja ámbar y no roja: no falló nada de lo que el usuario pidió, hay un dato
 * de menor calidad. El rojo está reservado para lo que sí falta (`PartialBanner`).
 *
 * Se nombra cadena por cadena, con la edad de cada una. «Algunos precios pueden
 * estar desactualizados» obligaría a desconfiar de las cuatro columnas para
 * cubrir una, que es justo la lectura que hay que evitar.
 */
export function StaleBanner({ chains, onRefresh }) {
  if (!chains?.length) return null;

  return (
    <div className="stale-note" role="status">
      <Clock size={15} strokeWidth={2} aria-hidden />
      <div>
        {chains.map((chain) => (
          <p key={chain.slug}>
            <span className="partial-chain">{chain.name}</span> no respondió: son
            sus precios de {formatAgeSeconds(chain.freshness?.age_seconds)} y
            pueden estar desactualizados.
          </p>
        ))}
        {onRefresh ? (
          <button className="stale-note-action" onClick={onRefresh}>
            Reintentar
          </button>
        ) : null}
      </div>
    </div>
  );
}
