import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Crosshair, MapPin, Search, X } from 'lucide-react';
import { geocodeAddress } from '../lib/api';
import './AddressLocator.css';

/**
 * Poner una dirección en el mapa.
 *
 * Lo usan los dos formularios que necesitan coordenadas —el de domicilios y el
 * de sucursales cargadas a mano— porque el problema es idéntico y la parte
 * delicada conviene resolverla una sola vez.
 *
 * **Muestra las opciones y no elige.** El buscador de direcciones del Estado
 * contesta con varias y las tres son igual de plausibles para él: «Av. Belgrano
 * 950» existe en Tres Arroyos, en Comuna 1 y en Saladillo. Elegir la primera es
 * lo que termina poniendo un marker a trescientos kilómetros con total
 * seguridad; quien está cargando su casa sabe cuál era.
 *
 * Y siempre queda la salida a mano. Geocodificar depende de una red que puede
 * no estar y de un callejero que no tiene todo: countries, calles nuevas,
 * direcciones sin altura. Pegar las coordenadas —las que se copian del mapa con
 * botón derecho, «¿qué hay aquí?»— no depende de nada y funciona siempre.
 */
export function AddressLocator({ value, address, city, province, onPick, onClear }) {
  const [candidates, setCandidates] = useState(null);
  const [status, setStatus] = useState('idle');
  const [error, setError] = useState(null);
  const [manual, setManual] = useState('');
  const [manualError, setManualError] = useState(null);
  const picksRef = useRef(null);

  const query = address?.trim() ?? '';

  // El formulario es largo y el botón de buscar queda arriba: sin esto, las
  // opciones aparecen abajo del pliegue y parece que la búsqueda no hizo nada.
  useEffect(() => {
    if (candidates?.length) {
      picksRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }, [candidates]);

  const search = useCallback(async () => {
    setStatus('loading');
    setError(null);
    setCandidates(null);
    try {
      const result = await geocodeAddress({ address: query, city, province });
      setCandidates(result.candidates);
      if (!result.candidates.length) setError(result.note);
      setStatus('idle');
    } catch (cause) {
      setError(cause.message);
      setStatus('idle');
    }
  }, [query, city, province]);

  const pick = (candidate) => {
    onPick({
      latitude: candidate.latitude,
      longitude: candidate.longitude,
      geo_source: 'geocoded',
      geo_label: candidate.label,
    });
    setCandidates(null);
  };

  const pickManual = () => {
    // Se acepta lo que se copia del mapa: «-34.6056, -58.4135» y también con
    // espacios de más o sin la coma. Rechazar por el formato a alguien que ya
    // tiene el dato correcto sería puro trámite.
    const parts = manual.split(/[\s,]+/).filter(Boolean).map(Number);
    if (parts.length !== 2 || parts.some(Number.isNaN)) {
      setManualError('Pegá las dos coordenadas: −34.6056, −58.4135');
      return;
    }
    const [latitude, longitude] = parts;
    if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) {
      setManualError('Esas coordenadas no existen. La latitud va primero.');
      return;
    }
    setManualError(null);
    onPick({
      latitude,
      longitude,
      geo_source: 'manual',
      geo_label: null,
    });
    setManual('');
  };

  return (
    <div className="loc">
      {value?.latitude != null ? (
        <div className="loc-set">
          <MapPin size={14} strokeWidth={2} aria-hidden />
          <div className="loc-set-main">
            {/* Se muestra cómo entendió la dirección el buscador, sin retocar:
                es lo único que deja notar que ubicó otra cosa. «AV BELGRANO
                950, Tres Arroyos» cuando querías Vicente López salta a la
                vista, y un «ubicada ✓» a secas lo escondería. */}
            <p className="loc-set-label">
              {value.geo_label ?? 'Punto marcado a mano'}
            </p>
            <p className="loc-set-coords num">
              {value.latitude.toFixed(5)}, {value.longitude.toFixed(5)}
            </p>
          </div>
          <button
            type="button"
            className="btn btn--icon"
            onClick={onClear}
            aria-label="Quitar la ubicación"
          >
            <X size={15} strokeWidth={1.75} aria-hidden />
          </button>
        </div>
      ) : null}

      <div className="loc-actions">
        <button
          type="button"
          className="btn btn--quiet"
          onClick={search}
          disabled={query.length < 3 || status === 'loading'}
        >
          <Search size={14} strokeWidth={2} aria-hidden />
          {status === 'loading'
            ? 'Buscando…'
            : value?.latitude != null
              ? 'Buscar de nuevo'
              : 'Ubicar en el mapa'}
        </button>
        {query.length < 3 ? (
          <span className="loc-hint">Escribí la calle y la altura.</span>
        ) : null}
      </div>

      {error ? <p className="loc-error">{error}</p> : null}

      {candidates?.length ? (
        <div className="loc-picks" ref={picksRef}>
          <p className="loc-picks-title">
            {candidates.length === 1
              ? 'Encontré esta:'
              : `Encontré ${candidates.length}. ¿Cuál es?`}
          </p>
          <ul className="loc-list">
            {candidates.map((candidate) => (
              <li key={`${candidate.latitude},${candidate.longitude}`}>
                <button
                  type="button"
                  className="loc-pick"
                  onClick={() => pick(candidate)}
                >
                  <Check size={14} strokeWidth={2} aria-hidden />
                  <span>{candidate.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <details className="loc-manual">
        <summary>
          <Crosshair size={13} strokeWidth={2} aria-hidden />
          Marcar el punto a mano
        </summary>
        <p className="loc-manual-help">
          En cualquier mapa, botón derecho sobre el lugar → «¿Qué hay aquí?», y
          pegá acá las dos coordenadas que aparecen.
        </p>
        <div className="loc-manual-row">
          <input
            className="input num"
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            placeholder="-34.6056, -58.4135"
            aria-label="Coordenadas"
          />
          <button type="button" className="btn btn--quiet" onClick={pickManual}>
            Usar
          </button>
        </div>
        {manualError ? <p className="loc-error">{manualError}</p> : null}
      </details>
    </div>
  );
}
