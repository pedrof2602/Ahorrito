import { CONFIDENCE_HINT, CONFIDENCE_LABEL } from '../lib/model';

/**
 * Cuánto se puede confiar en que las dos cadenas están comparando lo mismo.
 *
 * Solo `aproximado` se pinta. Es deliberado: es el único de los tres estados
 * que cambia lo que deberías hacer —mirar qué matcheó cada cadena, o fijar el
 * producto— y darle color a los otros dos convertiría el dato en decoración.
 */
export function ConfidenceTag({ confidence }) {
  const label = CONFIDENCE_LABEL[confidence];
  if (!label) return null;
  return (
    <span
      className={`chip ${confidence === 'name' ? 'chip--warn' : 'chip--bare'}`}
      title={CONFIDENCE_HINT[confidence]}
    >
      {label}
    </span>
  );
}
