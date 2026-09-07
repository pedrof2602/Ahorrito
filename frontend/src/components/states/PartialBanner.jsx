import { AlertTriangle } from 'lucide-react';
import { chainNameFromSlug } from '../../lib/model';
import './states.css';

/**
 * Una cadena falló y su total falta.
 *
 * Franja, no modal: la comparación que sí se pudo hacer sigue siendo útil y no
 * hay que taparla para avisar. Lo que no puede pasar es coronar un ganador con
 * un competidor caído — de eso se ocupa el veredicto, que deja de afirmarlo.
 */
export function PartialBanner({ errors }) {
  if (!errors?.length) return null;

  return (
    <div className="partial" role="status">
      <AlertTriangle size={15} strokeWidth={2} aria-hidden />
      <div>
        {errors.map((error) => (
          <p key={error.chain_slug}>
            <span className="partial-chain">
              {chainNameFromSlug(error.chain_slug)}
            </span>{' '}
            no respondió: {error.message}
          </p>
        ))}
      </div>
    </div>
  );
}
