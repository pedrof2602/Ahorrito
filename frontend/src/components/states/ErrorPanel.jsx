import { AlertCircle } from 'lucide-react';
import './states.css';

/**
 * Falló la consulta.
 *
 * Va **arriba** del último resultado bueno, nunca en su lugar: quedarse sin red
 * mientras leés los precios en la góndola no puede borrarte los precios de la
 * pantalla. Cuando hay datos viejos abajo, el panel lo dice, para que nadie tome
 * por fresco un número de hace tres horas.
 */
export function ErrorPanel({ message, onRetry, hasStaleData }) {
  return (
    <div className="errorpanel" role="alert">
      <AlertCircle size={18} strokeWidth={2} aria-hidden />
      <div>
        <p className="errorpanel-title">No se pudieron traer los precios</p>
        <p className="errorpanel-text">{message}</p>
        {hasStaleData ? (
          <p className="errorpanel-text">
            Abajo siguen los números de la última consulta que salió bien.
          </p>
        ) : null}
        <div className="errorpanel-actions">
          <button className="btn btn--ghost" onClick={onRetry}>
            Reintentar
          </button>
        </div>
      </div>
    </div>
  );
}
