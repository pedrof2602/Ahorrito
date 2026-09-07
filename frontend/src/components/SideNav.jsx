import { CreditCard, Diamond, ListChecks, MapPin, Settings } from 'lucide-react';
import './SideNav.css';

/**
 * La navegación de escritorio.
 *
 * Existe solo de 1040px en adelante. En un teléfono la app es una pantalla con
 * paneles que se abren encima —el gesto correcto con una mano y la lista en la
 * góndola— y una barra lateral ahí sería una columna de accesos comiéndose el
 * ancho que necesitan los precios. En una notebook pasa lo contrario: los
 * paneles quedan escondidos detrás de íconos que no dicen qué son, y sobra
 * ancho de sobra para nombrarlos.
 *
 * No son rutas: son los mismos paneles que abre la barra superior del
 * teléfono, con su nombre escrito. «Comparar hoy» es la pantalla, y por eso es
 * el único ítem que no abre nada.
 */
export function SideNav({ onOpenSheet, postalCode }) {
  const items = [
    { id: null, label: 'Comparar hoy', icon: Diamond },
    { id: 'list', label: 'Lista de compras', icon: ListChecks },
    { id: 'cards', label: 'Tarjetas y promos', icon: CreditCard },
    { id: 'settings', label: 'Configuración', icon: Settings },
  ];

  return (
    <aside className="sidenav">
      <div className="sidenav-brand">
        <span className="sidenav-mark" aria-hidden>
          C
        </span>
        Comparador
      </div>

      <nav className="sidenav-items" aria-label="Secciones">
        {items.map(({ id, label, icon: Icon }) => (
          <button
            key={label}
            className={`sidenav-item${id === null ? ' sidenav-item--on' : ''}`}
            onClick={() => (id ? onOpenSheet(id) : null)}
            aria-current={id === null ? 'page' : undefined}
          >
            <Icon size={16} strokeWidth={1.75} aria-hidden />
            {label}
          </button>
        ))}
      </nav>

      {/* Lo mismo que dice la fila de contexto en el teléfono: desde dónde se
          están resolviendo estos precios. Abre los ajustes, que es donde se
          cambia. */}
      <button className="sidenav-foot" onClick={() => onOpenSheet('settings')}>
        <MapPin size={13} strokeWidth={2} aria-hidden />
        {postalCode ? (
          <>
            CP <span className="num">{postalCode}</span>
          </>
        ) : (
          'Sin código postal'
        )}
      </button>
    </aside>
  );
}
