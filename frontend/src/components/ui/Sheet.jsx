import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import './ui.css';

/**
 * Panel deslizante. Es el único patrón de navegación de la app: no hay rutas
 * porque no hay pantallas, hay una pantalla y cosas que se abren encima.
 */
export function Sheet({ title, onClose, children, footer }) {
  const panelRef = useRef(null);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);

    // Sin bloquear el scroll del body, arrastrar dentro del sheet mueve la
    // página de atrás y al cerrar aparecés en otra parte de la lista.
    const { overflow } = document.body.style;
    document.body.style.overflow = 'hidden';

    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = overflow;
    };
  }, [onClose]);

  useEffect(() => {
    // El foco entra al panel para que el lector de pantalla anuncie el título y
    // para que Escape funcione sin tener que tocar nada primero.
    panelRef.current?.focus({ preventScroll: true });
  }, []);

  return createPortal(
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="sheet-head">
          <h2 className="sheet-title">{title}</h2>
          <button className="btn btn--icon" onClick={onClose} aria-label="Cerrar">
            <X size={20} strokeWidth={1.75} />
          </button>
        </header>
        <div className="sheet-body">{children}</div>
        {footer ? <footer className="sheet-foot">{footer}</footer> : null}
      </div>
    </>,
    document.body,
  );
}
