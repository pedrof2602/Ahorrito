/**
 * Pantalla de revisión visual — NO forma parte de la app.
 *
 * Monta `<App/>` con el `fetch` interceptado y datos fijos, para poder mirar el
 * layout (y sacarle capturas) sin levantar el backend ni pegarle a las APIs de
 * los supermercados. Se sirve desde `dev-preview.html`.
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import '@fontsource-variable/inter';
import '@fontsource-variable/sora/wght.css';
import './styles/tokens.css';
import './styles/base.css';
import './components/ui/ui.css';
import { App } from './App';

const CHAINS = [
  { slug: 'dia-ar', name: 'Supermercados DIA', total: 669200, base: 712400 },
  { slug: 'coto-ar', name: 'Coto', total: 688500, base: 688500 },
  { slug: 'carrefour-ar', name: 'Carrefour', total: 701300, base: 733000 },
  { slug: 'disco-ar', name: 'Disco', total: 742600, base: 742600 },
];

const PRODUCTS = [
  { query: 'Gaseosa Coca-Cola Sabor Original 1,25 Lt', qty: 2, conf: 'pinned', prices: [279700, 314000, 329000, 360000] },
  { query: 'leche entera 1L', qty: 3, conf: 'exact_ean', prices: [118500, 121000, 129000, 134000] },
  { query: 'yerba mate 1kg', qty: 1, conf: 'name', prices: [518000, 502000, 545000, 561000] },
  { query: 'aceite de girasol 1,5L', qty: 1, conf: 'exact_ean', prices: [65300, 68000, null, 72000] },
  { query: 'pan lactal integral', qty: 1, conf: 'name', prices: [42700, 44500, 46300, 48600] },
];

/* `?state=stale` simula que Disco no respondió y compitió con precios de ayer.
   Detrás de una bandera y no siempre: es el caso excepcional, y dejarlo prendido
   haría que la revisión visual del caso normal —el 99% de las veces— tenga
   siempre una franja de más. */
const STALE_DEMO = new URLSearchParams(location.search).get('state') === 'stale';

/** Hace cuánto se le preguntó a la cadena por estos precios. */
function freshness(slug) {
  const stale = STALE_DEMO && slug === 'disco-ar';
  return {
    fetched_at: new Date(Date.now() - (stale ? 27 : 2) * 3600 * 1000).toISOString(),
    age_seconds: (stale ? 27 : 2) * 3600,
    from_cache: true,
    stale,
  };
}

const comparison = {
  on_date: '2026-09-02',
  notes: [
    'El ranking sale del total comparable: solo suma las líneas que todas las cadenas tienen.',
    'Coto no publica checkout consultable, así que su total es estimado.',
  ],
  unverified_rules: [],
  chains: CHAINS.map((chain) => ({
    chain_slug: chain.slug,
    display_name: chain.name,
    best_final_total_cents: chain.total,
    base_total_cents: chain.base,
    best_instrument_label: 'Brubank NFC',
    simulated: chain.slug !== 'coto-ar',
  })),
  options: CHAINS.map((chain) => ({
    chain_slug: chain.slug,
    final_total_cents: chain.total,
    reference_total_cents: chain.base,
    total_saving_cents: chain.base - chain.total,
    out_of_pocket_today_cents: chain.total,
    confidence: chain.slug === 'coto-ar' ? 'estimated' : 'measured',
    bank_promo:
      chain.slug === 'dia-ar'
        ? { name: 'Brubank · 10% los martes', percent: 0.1, capped: true, saving_cents: 43200, unverified: false }
        : null,
    provider_promotions: [],
    caveats: [],
  })),
  basket: {
    partial: false,
    stale: STALE_DEMO,
    errors: [],
    comparable_line_count: 4,
    total_line_count: PRODUCTS.length,
    chains: CHAINS.map((chain) => ({
      chain_slug: chain.slug,
      matched_lines: chain.slug === 'carrefour-ar' ? 4 : 5,
      missing_lines: chain.slug === 'carrefour-ar' ? ['aceite de girasol 1,5L'] : [],
      price_scope: 'national',
      freshness: freshness(chain.slug),
      store: null,
    })),
    lines: PRODUCTS.map((product) => ({
      query: product.query,
      quantity: product.qty,
      confidence: product.conf,
      ean: product.conf === 'pinned' ? '7790895005312' : null,
      comparable: product.prices.every(Boolean),
      cheapest_chain: CHAINS[
        product.prices.reduce(
          (best, price, index) =>
            price != null && (product.prices[best] == null || price < product.prices[best])
              ? index
              : best,
          0,
        )
      ].slug,
      saving_cents: 0,
      missing_in: [],
      matches: product.prices
        .map((price, index) =>
          price == null
            ? null
            : {
                chain_slug: CHAINS[index].slug,
                line_total_cents: price,
                product: {
                  name: product.query,
                  brand: 'Marca',
                  measurement_unit: 'un',
                },
                offer: {
                  price_cents: Math.round(price / product.qty),
                  price_per_unit_cents: null,
                  price_scope: 'national',
                  available: true,
                },
              },
        )
        .filter(Boolean),
    })),
  },
};

const STORE_LOCATIONS = {
  center_latitude: -34.5722,
  center_longitude: -58.4356,
  center_source: 'postal_code',
  center_label: null,
  note: null,
  stores: [
    { chain_slug: 'dia-ar', external_id: '1', name: 'DIA Belgrano', address: 'Cabildo 1500', latitude: -34.5645, longitude: -58.4562, distance_km: 0.8, location_source: 'provider' },
    { chain_slug: 'coto-ar', external_id: '2', name: 'Coto Olleros', address: 'Olleros 1900', latitude: -34.5731, longitude: -58.4359, distance_km: 0.3, location_source: 'geocoded' },
    { chain_slug: 'carrefour-ar', external_id: '3', name: 'Carrefour Colegiales', address: 'Federico Lacroze 2200', latitude: -34.5766, longitude: -58.4462, distance_km: 1.1, location_source: 'provider' },
  ],
};

const ROUTES = [
  [/\/profile$/, { postal_code: '1425', channel: 'in_store', sales_channel: null, theme: 'dark', full_name: 'Pedro', dni: null, email: null, phone: null }],
  [/\/shopping-lists\/default$/, { id: 1, name: 'Mi lista', lines: PRODUCTS.map((p) => ({ query: p.query, quantity: p.qty, ean: null, pinned_label: null })) }],
  [/\/basket\/best-payment$/, comparison],
  [/\/store-locations/, STORE_LOCATIONS],
  [/\/payment-instruments$/, []],
  [/\/payment-issuers$/, []],
  [/\/addresses$/, []],
  [/\/custom-stores$/, []],
  [/\/chains$/, []],
];

window.fetch = async (url) => {
  const path = String(url);
  const hit = ROUTES.find(([pattern]) => pattern.test(path));
  return {
    ok: true,
    status: 200,
    json: async () => (hit ? hit[1] : {}),
  };
};

// La lista se siembra en la caché, no se espera del backend: el primer render
// sale de `localStorage` y es ese render el que dispara la comparación.
localStorage.clear();
localStorage.setItem(
  'compras.list.v1',
  JSON.stringify(PRODUCTS.map((p) => ({ query: p.query, quantity: p.qty }))),
);
localStorage.setItem(
  'compras.settings.v1',
  JSON.stringify({ postalCode: '1425', channel: 'in_store', theme: 'dark' }),
);
localStorage.setItem('compras.migrated.v1', JSON.stringify({ list: true, settings: true }));

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// `?scroll=600` deja la página desplazada: es la única forma de mirar cómo se
// comporta la cabecera pegajosa de la tabla en una captura.
const scrollTo = Number(new URLSearchParams(location.search).get('scroll'));
if (scrollTo) setTimeout(() => window.scrollTo(0, scrollTo), 900);

// `?tab=map` abre el mapa solo, para poder mirarlo sin poder hacer clic.
if (new URLSearchParams(location.search).get('tab') === 'map') {
  const open = () => {
    const tab = [...document.querySelectorAll('.detail-tabs .seg-btn')].find(
      (button) => button.textContent.trim() === 'Mapa',
    );
    if (tab) tab.click();
    else setTimeout(open, 200);
  };
  setTimeout(open, 600);
}
