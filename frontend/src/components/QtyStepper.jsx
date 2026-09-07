import { Minus, Plus } from 'lucide-react';

/**
 * Cantidad de una línea.
 *
 * Botones y no un campo de texto: se usa con una mano, y abrir el teclado
 * numérico para pasar de 2 a 3 es más trabajo del que vale.
 */
export function QtyStepper({ value, onChange, label }) {
  return (
    <div className="stepper">
      <button
        className="btn btn--icon stepper-btn"
        onClick={() => onChange(value - 1)}
        disabled={value <= 1}
        aria-label={`Quitar una unidad de ${label}`}
      >
        <Minus size={16} strokeWidth={2} aria-hidden />
      </button>
      <span className="stepper-value num" aria-live="polite">
        {value}
      </span>
      <button
        className="btn btn--icon stepper-btn"
        onClick={() => onChange(value + 1)}
        disabled={value >= 99}
        aria-label={`Agregar una unidad de ${label}`}
      >
        <Plus size={16} strokeWidth={2} aria-hidden />
      </button>
    </div>
  );
}
