import { useEffect, useRef, useState } from 'react';
import { Loader2, Search } from 'lucide-react';
import { searchProducts } from '../lib/api';
import { formatCents } from '../lib/money';
import { chainNameFromSlug } from '../lib/model';
import { Sheet } from './ui/Sheet';
import './ProductPicker.css';

/**
 * Elegir el producto exacto de una línea.
 *
 * Es lo que convierte una comparación orientativa en una exacta: mientras la
 * línea dice «leche», cada cadena aporta su mejor coincidencia por nombre y
 * pueden diferir en marca o gramaje. Al fijar un producto se guarda su código de
 * barras y las dos cadenas pasan a comparar lo mismo. Se hace una vez por
 * producto de los que comprás siempre, y queda.
 *
 * Solo se ofrecen los resultados **con EAN**: sin código de barras no hay nada
 * que fijar, y mostrarlos sugeriría lo contrario.
 */
export function ProductPicker({ line, postalCode, onPick, onClose }) {
  const [term, setTerm] = useState(line.query);
  const [results, setResults] = useState([]);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  const inputRef = useRef(null);
  const controllerRef = useRef(null);

  const run = async (query) => {
    const clean = query.trim();
    if (clean.length < 2) return;

    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setStatus('loading');
    setError(null);

    try {
      const data = await searchProducts(clean, {
        postalCode,
        signal: controller.signal,
      });
      setResults((data.results ?? []).filter((r) => r.product.ean));
      setStatus('success');
    } catch (cause) {
      if (cause?.name === 'AbortError') return;
      setError(cause.message);
      setStatus('error');
    }
  };

  // Se busca al abrir con el texto que ya tenía la línea: es casi siempre lo que
  // se quería buscar, y ahorra tipear lo mismo otra vez.
  useEffect(() => {
    // Ver la nota de `useInstruments`: buscar al abrir es sincronizar con un
    // sistema externo, no derivar estado.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    run(line.query);
    inputRef.current?.select();
    return () => controllerRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Sheet title="Fijar producto" onClose={onClose}>
      <form
        className="picker-search"
        onSubmit={(event) => {
          event.preventDefault();
          run(term);
        }}
      >
        <input
          ref={inputRef}
          className="input"
          value={term}
          onChange={(event) => setTerm(event.target.value)}
          placeholder="Buscar producto"
          enterKeyHint="search"
          autoFocus
        />
        <button
          className="btn btn--icon"
          type="submit"
          aria-label="Buscar"
          disabled={status === 'loading'}
        >
          {status === 'loading' ? (
            <Loader2 size={18} className="spin" aria-hidden />
          ) : (
            <Search size={18} strokeWidth={1.75} aria-hidden />
          )}
        </button>
      </form>

      {line.ean ? (
        <div className="picker-current">
          <span>Fijado: {line.pinnedLabel ?? line.query}</span>
          <button className="btn btn--quiet" onClick={() => onPick(null, null)}>
            Soltar
          </button>
        </div>
      ) : null}

      {status === 'error' ? <p className="picker-empty">{error}</p> : null}

      {status === 'success' && !results.length ? (
        <p className="picker-empty">
          Ningún resultado con código de barras para «{term}». Sin código no se
          puede fijar; la línea va a seguir comparándose por nombre.
        </p>
      ) : null}

      <ul className="picker-list">
        {results.map(({ product, offer }) => (
          <li key={`${product.chain_slug}-${product.sku_id}`}>
            <button
              className="picker-item"
              onClick={() => onPick(product.ean, product.name)}
            >
              {product.image_url ? (
                <img
                  className="picker-thumb"
                  src={product.image_url}
                  alt=""
                  loading="lazy"
                  width={40}
                  height={40}
                />
              ) : (
                <span className="picker-thumb picker-thumb--empty" aria-hidden />
              )}
              <span className="picker-body">
                <span className="picker-name">{product.name}</span>
                <span className="picker-meta">
                  {chainNameFromSlug(product.chain_slug)}
                  {product.brand ? ` · ${product.brand}` : ''}
                  {offer.available ? '' : ' · sin stock'}
                </span>
              </span>
              <span className="picker-price num">
                {formatCents(offer.price_cents)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Sheet>
  );
}
