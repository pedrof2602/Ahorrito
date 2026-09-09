import { lazy, Suspense, useMemo, useState } from 'react';
import { CreditCard, ListPlus, MapPin, RefreshCw } from 'lucide-react';
import { useShoppingList } from './hooks/useShoppingList';
import { useSettings, CHANNEL_LABEL } from './hooks/useSettings';
import { useComparison } from './hooks/useComparison';
import { useStoreLocations } from './hooks/useStoreLocations';
import { buildView } from './lib/model';
import { formatAge, formatAgeSeconds } from './lib/money';
import { Logo } from './components/ui/Logo';
import { SideNav } from './components/SideNav';
import { Verdict } from './components/Verdict';
import { ChainStrip } from './components/ChainStrip';
import { LineTable } from './components/LineTable';
import { TotalBreakdown } from './components/TotalBreakdown';
import { ListSheet } from './components/ListSheet';
import { SettingsSheet } from './components/SettingsSheet';
import { CardsSheet } from './components/CardsSheet';
import { AddressSheet } from './components/AddressSheet';
import { StoresSheet } from './components/StoresSheet';
import { Skeleton } from './components/states/Skeleton';
import { EmptyList } from './components/states/EmptyList';
import { ErrorPanel } from './components/states/ErrorPanel';
import { PartialBanner } from './components/states/PartialBanner';
import { StaleBanner } from './components/states/StaleBanner';
import './App.css';

/* Leaflet y sus estilos pesan ~150 KB sobre un bundle de 200. Cargarlos siempre
   encarecería el arranque de una app que se abre parada en la góndola, para una
   vista que puede no abrirse nunca. Se carga al tocar la pestaña. */
const MapView = lazy(() => import('./components/MapView'));

/** «Carrefour, Disco, Día y Coto» — enumeración como se escribe, no como se
    programa: una lista separada por comas termina en «y», no en otra coma. */
function listChains(names) {
  if (!names.length) return '';
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(', ')} y ${names[names.length - 1]}`;
}

export function App() {
  const list = useShoppingList();
  const { settings, update } = useSettings();
  const comparison = useComparison(list.lines, settings);
  const [sheet, setSheet] = useState(null);
  const [detail, setDetail] = useState('table');

  const view = useMemo(() => buildView(comparison.data), [comparison.data]);
  const locations = useStoreLocations(detail === 'map', settings.postalCode);
  const hasList = list.lines.length > 0;
  const loading = comparison.status === 'loading';

  /* De cuándo son los PRECIOS, no de cuándo pedimos la comparación. El backend
     cachea las consultas a las cadenas hasta seis horas, así que los dos números
     se separaron: se puede haber pedido recién y recibir precios de hace cinco
     horas, y el que importa para decidir dónde comprar es el segundo.
     `fetchedAt` queda de respaldo para el resultado guardado en localStorage,
     que puede ser de antes de que existiera `freshness`. */
  const age =
    formatAgeSeconds(view?.freshness?.age_seconds) ??
    formatAge(comparison.fetchedAt);

  /* Ámbar en la barra de contexto si la lista cambió (lo de siempre) o si algún
     precio se sirvió vencido porque su cadena no respondió. */
  const ageIsStale = comparison.isStale || Boolean(view?.stale);

  const compareNow = () => {
    setSheet(null);
    comparison.refresh();
  };

  const chainNames = view
    ? listChains([
      ...view.chains.map((chain) => chain.name),
      ...view.failedChains.map((chain) => chain.name),
    ])
    : '';

  return (
    <div className="shell">
      <SideNav onOpenSheet={setSheet} postalCode={settings.postalCode} />

      <div className="shell-body">
        {/* La barra del teléfono. De 1040px en adelante su lugar lo toman la
            barra lateral y el encabezado de acá abajo, que dicen lo mismo con
            el ancho que hay para escribirlo. */}
        <header className="topbar">
          <div className="topbar-row">
            <h1 className="topbar-title">
              <Logo size={24} />
              {/* En pantallas muy angostas el nombre cede el lugar a los
                  controles: la marca sola ya identifica la app. */}
              <span className="topbar-wordmark">Ahorrito</span>
            </h1>
            <div className="topbar-actions">
              <button
                className="btn btn--icon"
                onClick={comparison.refreshLive}
                disabled={loading || !hasList}
                aria-label="Actualizar precios"
              >
                <RefreshCw
                  size={18}
                  strokeWidth={1.75}
                  className={loading ? 'spin' : undefined}
                  aria-hidden
                />
              </button>
              <button
                className="btn btn--icon"
                onClick={() => setSheet('cards')}
                aria-label="Mis tarjetas"
              >
                <CreditCard size={18} strokeWidth={1.75} aria-hidden />
              </button>
              <button className="btn btn--ghost topbar-list" onClick={() => setSheet('list')}>
                <ListPlus size={17} strokeWidth={1.75} aria-hidden />
                Lista
                {hasList ? <span className="num">{list.lines.length}</span> : null}
              </button>
            </div>
          </div>

          <button className="topbar-context" onClick={() => setSheet('settings')}>
            <span>{settings.postalCode ? `CP ${settings.postalCode}` : 'Sin CP'}</span>
            <span className="topbar-dot">·</span>
            <span>{CHANNEL_LABEL[settings.channel]}</span>
            {age ? (
              <>
                <span className="topbar-dot">·</span>
                <span className={ageIsStale ? 'topbar-stale' : undefined}>
                  {age}
                </span>
              </>
            ) : null}
          </button>
        </header>

        {/* Encabezado de escritorio: qué estás mirando y de dónde salen estos
            precios, que en el teléfono lo dice la fila de contexto. */}
        <div className="pagehead">
          <div className="pagehead-main">
            <h2 className="pagehead-title">Tu compra de hoy</h2>
            <p className="pagehead-sub">
              {hasList ? (
                <>
                  <span className="num">{list.lines.length}</span>{' '}
                  {list.lines.length === 1 ? 'producto' : 'productos'}
                </>
              ) : (
                'Sin productos en la lista'
              )}
              {chainNames ? ` · comparando ${chainNames}` : ''}
              {age ? (
                <>
                  {' · '}
                  <span className={ageIsStale ? 'topbar-stale' : undefined}>
                    {age}
                  </span>
                </>
              ) : null}
            </p>
          </div>

          <div className="pagehead-actions">
            <button
              className="btn btn--icon"
              onClick={comparison.refreshLive}
              disabled={loading || !hasList}
              aria-label="Actualizar precios"
            >
              <RefreshCw
                size={17}
                strokeWidth={1.75}
                className={loading ? 'spin' : undefined}
                aria-hidden
              />
            </button>
            <button className="pagehead-cp" onClick={() => setSheet('settings')}>
              <MapPin size={13} strokeWidth={2} aria-hidden />
              {settings.postalCode ? (
                <>
                  CP <span className="num">{settings.postalCode}</span>
                </>
              ) : (
                'Sin CP'
              )}
            </button>
          </div>
        </div>

        <main>
          {/* La lista cambió y los números de abajo describen la anterior. Se
              avisa en vez de refrescar solo: cada consulta pega contra las APIs
              reales de los supermercados y tarda. */}
          {comparison.listChanged && !loading ? (
            <button className="stalebar" onClick={comparison.refresh}>
              <RefreshCw size={14} strokeWidth={2} aria-hidden />
              La lista cambió — actualizar precios
            </button>
          ) : null}

          {comparison.error ? (
            <ErrorPanel
              message={comparison.error}
              onRetry={comparison.refresh}
              hasStaleData={Boolean(view)}
            />
          ) : null}

          {!hasList ? (
            <EmptyList onOpenList={() => setSheet('list')} />
          ) : loading && !view ? (
            <Skeleton rows={list.lines.length} />
          ) : view ? (
            <div className={loading ? 'is-refreshing' : undefined}>
              <PartialBanner errors={view.errors} />
              {/* Debajo del de cadena caída: ese dice qué falta, este que algo
                  de lo que hay vale menos. Ese orden es el de la gravedad.
                  No incluye a la ganadora: de esa habla el veredicto. */}
              <StaleBanner
                chains={view.staleBannerChains}
                onRefresh={loading ? null : comparison.refreshLive}
              />

              {/* Hasta 1040px es una columna, en el orden de siempre: primero el
                  veredicto, después el detalle. De ahí en adelante la respuesta
                  se acomoda arriba —las cadenas en fila, el veredicto como una
                  sola línea— y el detalle se queda con el ancho entero, que es
                  lo que necesitan cuatro columnas de precios y un mapa. Todo
                  eso lo resuelve el CSS: el árbol es el mismo en los dos. */}
              <div className="layout">
                {/* El orden del DOM es el del teléfono —la respuesta primero,
                    el desglose por cadena después— y en escritorio se invierte
                    con `order`: ahí las cuatro tarjetas encabezan y el
                    veredicto queda debajo como una sola línea que las resume. */}
                <div className="layout-answer">
                  <Verdict view={view} />
                  <ChainStrip view={view} />
                </div>
                <div className="layout-detail">
                  {/* Dos formas de leer el mismo resultado: qué me llevo, o dónde
                      lo compro. */}
                  <div
                    className="seg detail-tabs"
                    role="radiogroup"
                    aria-label="Cómo ver el detalle"
                  >
                    {[
                      ['table', 'Lista'],
                      ['map', 'Mapa'],
                    ].map(([value, label]) => (
                      <button
                        key={value}
                        role="radio"
                        aria-checked={detail === value}
                        className={`seg-btn${detail === value ? ' seg-btn--on' : ''
                          }`}
                        onClick={() => setDetail(value)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>

                  {detail === 'table' ? (
                    <LineTable view={view} />
                  ) : (
                    // El fallback no puede usar una clase de MapView.css: ese CSS
                    // viaja con el chunk que todavía se está bajando.
                    <Suspense
                      fallback={<p className="detail-loading">Cargando el mapa…</p>}
                    >
                      <MapView
                        view={view}
                        locations={locations}
                        theme={settings.theme}
                        onAddStore={() => setSheet('stores')}
                      />
                    </Suspense>
                  )}
                </div>
              </div>

              <TotalBreakdown chain={view.winner} />

              {view.notes.length ? (
                <ul className="notes">
                  {view.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </main>
      </div>

      {sheet === 'list' ? (
        <ListSheet
          list={list}
          postalCode={settings.postalCode}
          dirty={comparison.listChanged || !view}
          onClose={() => setSheet(null)}
          onCompare={compareNow}
        />
      ) : null}

      {sheet === 'settings' ? (
        <SettingsSheet
          settings={settings}
          onUpdate={update}
          onClose={() => setSheet(null)}
          onApply={comparison.refresh}
          onOpenAddresses={() => setSheet('addresses')}
          onOpenStores={() => setSheet('stores')}
        />
      ) : null}

      {sheet === 'cards' ? (
        <CardsSheet onClose={() => setSheet(null)} onChanged={comparison.refresh} />
      ) : null}

      {/* Se vuelve a ajustes y no se cierra: se llegó desde ahí. */}
      {sheet === 'addresses' ? (
        <AddressSheet onClose={() => setSheet('settings')} />
      ) : null}

      {sheet === 'stores' ? (
        <StoresSheet
          onClose={() => setSheet(null)}
          // El mapa cachea las ubicaciones mientras la pestaña sigue abierta;
          // sin este aviso, la sucursal recién cargada no aparecería hasta
          // recargar la página.
          onChanged={locations.reload}
        />
      ) : null}
    </div>
  );
}

export default App;
