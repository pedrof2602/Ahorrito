import { ShoppingCart } from 'lucide-react';
import './ui.css';

/**
 * El logotipo: carrito blanco sobre cuadrado verde.
 *
 * Vive en un componente y no repetido en cada lugar que lo usa porque aparece
 * en dos sitios que no se ven entre sí —la barra lateral de escritorio y la
 * pantalla de login— y una marca que se dibuja distinto en cada uno deja de ser
 * una marca.
 *
 * El carrito es el `ShoppingCart` de lucide, la misma familia de íconos que usa
 * el resto de la app. No es una casualidad estética: importarlo en vez de pegar
 * un `<path>` propio significa que el trazo, los remates redondeados y el
 * grosor son exactamente los del resto de la interfaz.
 *
 * **Hay una copia de esto en `public/favicon.svg`** y no se puede evitar: el
 * favicon lo pide el navegador como archivo suelto, antes de que exista React.
 * Si cambia el dibujo, hay que tocar los dos.
 */
export function Logo({ size = 24, className = '' }) {
  return (
    <span
      className={`logo ${className}`.trim()}
      style={{ '--logo-size': `${size}px` }}
      aria-hidden
    >
      <ShoppingCart
        /* El ícono ocupa el 60% del cuadrado. Es la proporción de la imagen
           original y la que deja el carrito respirando dentro del borde
           redondeado en vez de tocarlo. */
        size={Math.round(size * 0.6)}
        strokeWidth={2}
        absoluteStrokeWidth
      />
    </span>
  );
}
