import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MapContainer, Marker, Popup, TileLayer, useMap } from 'react-leaflet';
import L from 'leaflet';
import { MapPin } from 'lucide-react';
import { formatCents } from '../lib/money';
import { chainColor, SCOPE_NOTE } from '../lib/model';
import 'leaflet/dist/leaflet.css';
import './MapView.css';

/**
 * Dónde comprar, sobre el mapa.
 *
 * **No calcula nada.** Recibe el mismo `view` que arma la tabla y usa sus
 * `isCheapest` y `total` tal como vienen: si el mapa recalculara cuál conviene,
 * podría contradecir a la tabla que está a diez centímetros, y no habría forma
 * de saber cuál de las dos creer.
 *
 * Las ubicaciones vienen aparte, del endpoint de sucursales, porque no toda
 * cadena cotiza desde una sucursal: Disco y Día publican precio nacional. En
 * esos casos el marker contesta *dónde queda el súper*, y el popup aclara que el
 * precio no es el de ese local puntual.
 */

const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

/**
 * Proveedores de tiles, en orden de preferencia. Todos gratuitos y sin API key.
 *
 * Hay más de uno porque **no alcanza con que el proveedor funcione: tiene que
 * llegar desde la red del usuario**. `tile.openstreetmap.org` resuelve a cuatro
 * IPs de Fastly (`151.101.*.91`) que algunos ISP argentinos tienen bloqueadas por
 * rango; desde esa conexión los tiles nunca cargan y Leaflet, que los deja en
 * `visibility:hidden` hasta que llegan, deja un rectángulo vacío que parece un
 * bug de la app. Verificado que es bloqueo por IP y no por dominio: pedirle a
 * `www.openstreetmap.org` —que responde 200— por una de esas IPs también corta.
 *
 * El orden no es arbitrario. Primero el OSM canónico, que es el que se pidió y
 * el que la mayoría de las redes alcanza. Después el mirror de OSM France, que
 * sigue siendo OpenStreetMap —misma data, mismo estilo, etiquetas en español— y
 * vive en otra red. Esri queda último: es el más rápido de los tres (medido:
 * ~0,08 s por tile contra ~1,2 s de OSM France) pero ya no es OpenStreetMap, así
 * que es la red de emergencia y no la opción por defecto. Si te resulta muy lento
 * el segundo, subir Esri acá arriba es cambiar el orden de este array.
 *
 * CARTO no está en la lista a propósito: su basemap serviría, pero **hoy exige
 * API key** y devuelve los tiles con «API KEY REQUIRED» estampado encima (lo
 * comprobé mirando el PNG, no leyendo la doc). Por eso el tema oscuro se resuelve
 * invirtiendo los tiles claros por CSS, que no agrega dependencia ni credencial:
 * lo hace `.map--dark` en la hoja de al lado, sobre el panel de tiles y no sobre
 * el mapa entero, para que los markers no se inviertan con el fondo.
 */
const TILE_SOURCES = [
  {
    id: 'osm',
    name: 'OpenStreetMap',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: OSM_ATTRIBUTION,
  },
  {
    id: 'osm-fr',
    name: 'OpenStreetMap France',
    url: 'https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png',
    subdomains: 'abc',
    attribution: `${OSM_ATTRIBUTION} · tiles de OSM France`,
  },
  {
    id: 'esri',
    name: 'Esri',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
    attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a>',
    maxZoom: 19,
  },
];

/** Cuánto se espera a que aparezca el primer tile antes de cambiar de proveedor. */
const TILE_TIMEOUT_MS = 6000;

/**
 * Un proveedor de tiles, que avisa si no está sirviendo.
 *
 * El corte por tiempo es lo que de verdad hace falta, y no basta con escuchar
 * `tileerror`: cuando el bloqueo es a nivel IP la conexión no se rechaza, se
 * queda colgada, así que el evento de error puede tardar más de un minuto en
 * llegar o no llegar nunca. Con un temporizador, en cambio, la ausencia de un
 * primer tile ya es señal suficiente. `tileerror` solo sirve para adelantar el
 * cambio cuando el fallo sí es rápido (404, DNS, certificado).
 */
function TileSource({ source, onFail }) {
  const layerRef = useRef(null);

  useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return undefined;

    let done = false;
    let errors = 0;
    const give = () => {
      if (done) return;
      done = true;
      onFail(source.id);
    };
    const ok = () => {
      done = true;
      clearTimeout(timer);
    };
    // Un solo tile con error puede ser un hueco del proveedor; varios seguidos y
    // ninguno bueno es el proveedor entero.
    const bad = () => {
      if (++errors >= 4) give();
    };

    const timer = setTimeout(give, TILE_TIMEOUT_MS);
    layer.on('tileload', ok);
    layer.on('tileerror', bad);
    return () => {
      clearTimeout(timer);
      layer.off('tileload', ok);
      layer.off('tileerror', bad);
    };
  }, [source, onFail]);

  return (
    <TileLayer
      ref={layerRef}
      url={source.url}
      attribution={source.attribution}
      subdomains={source.subdomains ?? 'abc'}
      maxZoom={source.maxZoom ?? 19}
    />
  );
}

/** El tema puesto, resolviendo `system` contra lo que dice el sistema. */
function resolveTheme(theme) {
  if (theme === 'light' || theme === 'dark') return theme;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

/**
 * Etiqueta corta del marker, sacada del slug y no del nombre.
 *
 * Del nombre saldría mal: «Supermercados DIA» empieza con la palabra que no
 * identifica nada y daría `SUP`, mientras que el slug ya es el identificador
 * corto de la cadena. Tres letras y no dos porque `dia-ar` y `disco-ar` colisionan
 * en dos.
 */
function pinLabel(slug) {
  return slug.replace(/-ar$/, '').slice(0, 3).toUpperCase();
}

/**
 * Marker pintado con los tokens del proyecto.
 *
 * Es un `divIcon` y no el marker por defecto de Leaflet por dos motivos: el
 * ícono que trae referencia PNGs por ruta relativa y se rompe al empaquetar, y
 * un div se pinta con CSS — así el verde del más barato es literalmente la misma
 * variable `--win` que usa el chip «más barato» del resto de la app, incluido su
 * valor distinto en tema oscuro.
 */
function pin(label, isCheapest) {
  return L.divIcon({
    className: '',
    html: `<span class="map-pin${isCheapest ? ' map-pin--win' : ''}">${label}</span>`,
    iconSize: [38, 24],
    iconAnchor: [19, 12],
    popupAnchor: [0, -14],
  });
}

/**
 * El punto desde el que se miden las distancias.
 *
 * Sin esto, «a 0,4 km» es un número sin referencia: no se ve desde dónde, y por
 * lo tanto no se puede notar que el mapa está midiendo desde el lugar
 * equivocado. Con el punto dibujado, un centro mal ubicado se ve de una.
 */
const HOME_ICON = L.divIcon({
  className: '',
  html: '<span class="map-home"></span>',
  iconSize: [14, 14],
  iconAnchor: [7, 7],
  popupAnchor: [0, -8],
});

const CENTER_NOTE = {
  address: (label) => `Distancias medidas desde ${label ?? 'tu domicilio'}.`,
  explicit: () => 'Distancias medidas desde el punto que marcaste.',
  postal_code: () =>
    'Distancias medidas desde el centro aproximado de tu código postal. Cargá ' +
    'tu dirección en «Mis domicilios» para medirlas desde tu puerta.',
  geocoded: () => 'Distancias medidas desde el centro aproximado de tu código postal.',
};

/** Encuadra el mapa sobre los markers. */
function FitBounds({ points }) {
  const map = useMap();
  useEffect(() => {
    if (!points.length) return;
    // Encuadrar y no fijar centro y zoom: así entran todos sin importar cuán
    // dispersos estén, y no hay que acertar un nivel de zoom a mano.
    map.fitBounds(points, { padding: [40, 40], maxZoom: 15 });
  }, [map, points]);
  return null;
}

export function MapView({ view, locations, theme, onAddStore }) {
  const { center, centerSource, centerLabel, stores, note, status, error, reload } =
    locations;

  // Qué proveedor de tiles se está usando. Avanza solo cuando el anterior no
  // sirve; `TILE_SOURCES.length` significa que se acabaron los tres.
  const [tileIndex, setTileIndex] = useState(0);
  const onTileFail = useCallback((failedId) => {
    setTileIndex((current) =>
      // Compara el id y no el índice: si dos capas alcanzan a fallar (por
      // ejemplo al remontar el mapa), sin esto se saltearían dos proveedores.
      TILE_SOURCES[current]?.id === failedId ? current + 1 : current,
    );
  }, []);
  const tiles = TILE_SOURCES[tileIndex] ?? null;

  /** Cada cadena del ranking, con su ubicación al lado si la hay. */
  const markers = useMemo(() => {
    const byChain = new Map(stores.map((store) => [store.chain_slug, store]));
    return view.chains
      .map((chain) => {
        const store = byChain.get(chain.slug);
        if (!store) return null;
        // ¿El precio salió de *esta* sucursal, o es la más cercana y el precio
        // vino de otro lado? Es la diferencia entre informar y afirmar de más.
        const isPricedHere =
          chain.priceScope === 'store' &&
          chain.store?.external_id === store.external_id;
        return { chain, store, isPricedHere };
      })
      .filter(Boolean);
  }, [view.chains, stores]);

  // El centro entra en el encuadre junto con los markers: si queda fuera del
  // marco, la referencia de las distancias no se ve y el punto no sirve de nada.
  const points = useMemo(() => {
    const spots = markers.map(({ store }) => [store.latitude, store.longitude]);
    return center ? [...spots, center] : spots;
  }, [markers, center]);

  if (status === 'loading') {
    return <p className="map-note">Buscando sucursales…</p>;
  }

  if (status === 'error') {
    return (
      <div className="map-note">
        <p>{error}</p>
        <button className="btn btn--quiet" onClick={reload}>
          Reintentar
        </button>
      </div>
    );
  }

  if (!markers.length) {
    return (
      <div className="map-note">
        <p>
          {note ??
            'Ninguna de las cadenas de esta comparación tiene sucursales ubicadas.'}
        </p>
        {/* Es justo el momento en que cargar una a mano resuelve el problema, y
            mandar a buscar el panel a los ajustes sería esconder la salida. */}
        {onAddStore ? (
          <button className="btn btn--quiet" onClick={onAddStore}>
            <MapPin size={14} strokeWidth={2} aria-hidden />
            Cargar una sucursal a mano
          </button>
        ) : null}
      </div>
    );
  }

  const isDark = resolveTheme(theme) === 'dark';
  return (
    <div className={`map${isDark ? ' map--dark' : ''}`}>
      <div className="map-split">
      <MapContainer
        className="map-canvas"
        center={center ?? points[0]}
        zoom={13}
        scrollWheelZoom={false}
        aria-label="Sucursales de la comparación"
      >
        {tiles ? (
          // `key` para que al cambiar de proveedor Leaflet tire la capa vieja en
          // vez de reusarla: sin esto quedarían los tiles fallados en pantalla.
          <TileSource key={tiles.id} source={tiles} onFail={onTileFail} />
        ) : null}
        <FitBounds points={points} />

        {center ? (
          <Marker position={center} icon={HOME_ICON} zIndexOffset={-100}>
            <Popup>
              <p className="map-pop-store">
                {CENTER_NOTE[centerSource]?.(centerLabel) ?? 'Tu ubicación.'}
              </p>
            </Popup>
          </Marker>
        ) : null}

        {markers.map(({ chain, store, isPricedHere }) => (
          <Marker
            key={chain.slug}
            position={[store.latitude, store.longitude]}
            icon={pin(pinLabel(chain.slug), chain.isCheapest)}
          >
            <Popup>
              <p className="map-pop-chain">
                {chain.name}
                {chain.isCheapest ? (
                  <span className="chip chip--win">más barato</span>
                ) : null}
              </p>
              <p className="map-pop-total num">{formatCents(chain.total)}</p>
              <p className="map-pop-store">{store.name}</p>
              {store.address ? (
                <p className="map-pop-addr">
                  {store.address}
                  {store.distance_km != null
                    ? ` · a ${store.distance_km.toLocaleString('es-AR')} km`
                    : ''}
                </p>
              ) : null}

              {/* Sin esto el mapa estaría afirmando que ese total es el precio
                  de ese local, que es justo lo que el resto de la app se cuida
                  de no hacer. */}
              {!isPricedHere ? (
                <p className="map-pop-caveat">
                  {SCOPE_NOTE[chain.priceScope] ??
                    'El precio no es el de esta sucursal.'}
                </p>
              ) : null}
              {store.location_source === 'geocoded' ? (
                <p className="map-pop-caveat">Ubicación aproximada.</p>
              ) : null}
              {store.location_source === 'manual' ? (
                <p className="map-pop-caveat">
                  Sucursal que cargaste vos. Se usa en lugar de la que ubicó la
                  app para esta cadena.
                </p>
              ) : null}
            </Popup>
          </Marker>
        ))}
      </MapContainer>

        {/* La misma información que los markers, en texto. Un marker contesta
            «dónde», pero para elegir hace falta leer dirección, total y a qué
            distancia queda — y eso en un globo que hay que abrir de a uno no
            se puede comparar. En pantalla angosta la lista va debajo del
            mapa; de 1040px en adelante, al costado. */}
        <ul className="map-list">
          {markers.map(({ chain, store }) => (
            <li
              className={`map-store${chain.isCheapest ? ' map-store--win' : ''}`}
              key={chain.slug}
            >
              <p className="map-store-chain">
                <span
                  className="wallet-dot"
                  style={{ '--dot': chainColor(chain.slug) }}
                  aria-hidden
                />
                {store.name}
              </p>
              {store.address ? (
                <p className="map-store-addr">{store.address}</p>
              ) : null}
              <p
                className={`map-store-total num${
                  chain.isCheapest ? ' map-store-total--win' : ''
                }`}
              >
                {formatCents(chain.total)}
              </p>
              {store.distance_km != null ? (
                <p className="map-store-dist">
                  a <span className="num">{store.distance_km.toLocaleString('es-AR')}</span> km
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      </div>

      {/* Un mapa en blanco parece una app rota. Si los tiles no llegan se dice
          por qué, en vez de dejar un rectángulo vacío con markers flotando. */}
      {tiles === null ? (
        <p className="map-warn">
          No se pudo cargar el fondo del mapa desde ninguno de los proveedores.
          Suele ser un bloqueo de tu red o de tu proveedor de internet, no un
          problema de la app: las sucursales y los totales de acá abajo siguen
          siendo correctos.
        </p>
      ) : tileIndex > 0 ? (
        <p className="map-missing">
          OpenStreetMap no responde desde esta red; el fondo del mapa se está
          cargando desde {tiles.name}.
        </p>
      ) : null}

      {/* Las cadenas que sí tienen total pero no se pueden ubicar no se
          esconden: que no sepamos dónde queda Disco no borra su precio. */}
      {markers.length < view.chains.length ? (
        <p className="map-missing">
          Sin ubicación:{' '}
          {view.chains
            .filter((chain) => !markers.some((m) => m.chain.slug === chain.slug))
            .map((chain) => `${chain.name} (${formatCents(chain.total)})`)
            .join(' · ')}
        </p>
      ) : null}

      {/* Desde dónde se mide la cercanía. Va en pantalla y no solo en el popup
          del punto porque cambia el sentido de todas las distancias de arriba,
          y porque es donde se descubre que cargar la dirección sirve. */}
      {centerSource ? (
        <p className="map-missing">{CENTER_NOTE[centerSource]?.(centerLabel)}</p>
      ) : null}

      {note ? <p className="map-missing">{note}</p> : null}

      {onAddStore ? (
        <button className="btn btn--quiet map-add" onClick={onAddStore}>
          <MapPin size={14} strokeWidth={2} aria-hidden />
          ¿Falta un súper que tenés cerca?
        </button>
      ) : null}
    </div>
  );
}

export default MapView;
