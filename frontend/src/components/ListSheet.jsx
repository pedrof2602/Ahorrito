import { useRef, useState } from 'react';
import { Crosshair, Plus, Trash2 } from 'lucide-react';
import { Sheet } from './ui/Sheet';
import { QtyStepper } from './QtyStepper';
import { ProductPicker } from './ProductPicker';
import './ListSheet.css';

/**
 * Editar la lista de compras.
 *
 * Texto libre, como se escribe una lista en un papel: el backend resuelve
 * «leche entera 1L» contra el catálogo de cada cadena. El botón de fijar, por
 * línea, es para cuando esa resolución no alcanza.
 *
 * La lista se guarda mientras escribís; el botón de abajo cierra y dispara la
 * comparación, que es lo caro y por eso no pasa sola.
 */
export function ListSheet({ list, postalCode, onClose, onCompare, dirty }) {
  const [draft, setDraft] = useState('');
  const [pickingId, setPickingId] = useState(null);
  const inputRef = useRef(null);

  const picking = list.lines.find((line) => line.id === pickingId) ?? null;

  const submit = (event) => {
    event.preventDefault();
    const value = draft.trim();
    if (!value) return;
    list.add(value);
    setDraft('');
    // El foco se queda en el campo: una lista se carga de corrido, no de a un
    // producto por visita al teclado.
    inputRef.current?.focus();
  };

  return (
    <>
      <Sheet
        title="Mi lista"
        onClose={onClose}
        footer={
          <button
            className="btn btn--primary btn--block"
            onClick={onCompare}
            disabled={!list.lines.length}
          >
            {dirty ? 'Comparar precios' : 'Listo'}
          </button>
        }
      >
        <form className="listsheet-add" onSubmit={submit}>
          <input
            ref={inputRef}
            className="input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Leche entera 1L"
            aria-label="Agregar producto"
            enterKeyHint="done"
            autoFocus
          />
          <button
            className="btn btn--icon"
            type="submit"
            aria-label="Agregar"
            disabled={!draft.trim()}
          >
            <Plus size={20} strokeWidth={2} aria-hidden />
          </button>
        </form>

        {list.lines.length ? (
          <ul className="listsheet-lines">
            {list.lines.map((line) => (
              <li className="listsheet-line" key={line.id}>
                {/* El texto ocupa su propia fila, entero. Compartir la fila con
                    los controles lo dejaba en 150px en un teléfono, y una lista
                    donde no podés leer «arroz largo fino 1…» no es una lista. */}
                <input
                  className="listsheet-line-input"
                  value={line.query}
                  onChange={(event) => list.setQuery(line.id, event.target.value)}
                  aria-label={`Editar ${line.query}`}
                />

                <div className="listsheet-line-controls">
                  {line.ean ? (
                    <span className="chip chip--win listsheet-pinned">
                      producto fijado
                    </span>
                  ) : null}

                  <QtyStepper
                    value={line.quantity}
                    label={line.query}
                    onChange={(qty) => list.setQuantity(line.id, qty)}
                  />

                  <button
                    className={`btn btn--icon${line.ean ? ' listsheet-pin--on' : ''}`}
                    onClick={() => setPickingId(line.id)}
                    aria-label={`Fijar el producto exacto de ${line.query}`}
                    title="Fijar el producto exacto"
                  >
                    <Crosshair size={17} strokeWidth={1.75} aria-hidden />
                  </button>

                  <button
                    className="btn btn--icon"
                    onClick={() => list.remove(line.id)}
                    aria-label={`Quitar ${line.query}`}
                  >
                    <Trash2 size={17} strokeWidth={1.75} aria-hidden />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="listsheet-empty">
            Escribí lo que vas a comprar, uno por línea. Podés ser específico
            («yerba mate 1kg») o general («arroz»).
          </p>
        )}

        {list.lines.length ? (
          <div className="listsheet-foot">
            <span className="listsheet-count">
              {list.lines.length} de 50 líneas
            </span>
            <button className="btn btn--quiet" onClick={list.clear}>
              Vaciar
            </button>
          </div>
        ) : null}
      </Sheet>

      {picking ? (
        <ProductPicker
          line={picking}
          postalCode={postalCode}
          onClose={() => setPickingId(null)}
          onPick={(ean, label) => {
            if (ean) list.pin(picking.id, ean, label);
            else list.unpin(picking.id);
            setPickingId(null);
          }}
        />
      ) : null}
    </>
  );
}
