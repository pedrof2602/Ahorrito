import './states.css';

/**
 * Lista vacía.
 *
 * Sin ilustración ni onboarding: es una herramienta que ya sabés usar, y lo
 * único que falta es la lista. El botón abre el editor con el campo enfocado.
 */
export function EmptyList({ onOpenList }) {
  return (
    <div className="empty">
      <p className="empty-title">Todavía no hay lista</p>
      <p className="empty-text">
        Agregá lo que vas a comprar y te digo en qué súper sale más barato,
        con el descuento de tus tarjetas ya aplicado.
      </p>
      <div className="empty-action">
        <button className="btn btn--primary" onClick={onOpenList}>
          Armar la lista
        </button>
      </div>
    </div>
  );
}
