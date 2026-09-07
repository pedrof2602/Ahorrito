/**
 * Traduce la respuesta del backend a lo que la pantalla necesita.
 *
 * Existe para que los componentes no tengan que cruzar `chains` (ranking de
 * pagos), `basket.chains` (cobertura y ámbito del precio) y `basket.lines`
 * (detalle) cada vez que renderizan. Es también donde viven las reglas de
 * honestidad, en un solo lugar y no repartidas por el árbol.
 *
 * La más importante: **una cadena a la que le faltan productos tiene un total
 * más bajo *por faltarle productos*.** El backend ya no la deja encabezar el
 * ranking por eso —ordena primero por cuántas líneas resuelve—, así que
 * `winnerHasGaps` solo da true cuando *ninguna* cadena resolvió la lista
 * entera. Cuando pasa, la UI deja de afirmar un ahorro que no puede sostener:
 * los dos totales que compara están incompletos.
 *
 * Un producto sin stock cuenta como faltante: no poder llevártelo y no tenerlo
 * son lo mismo del lado del changuito.
 */

/**
 * Rótulo corto de la cadena para la cabecera de las columnas.
 *
 * Sale del slug y no del nombre porque el nombre empieza por lo que no
 * identifica: «Supermercados DIA» recortado da `SUPERM.`, que es lo mismo para
 * DIA que para cualquier otra cadena que se llame «Supermercados algo». El
 * slug ya es el identificador corto, y da `DIA`.
 */
export function chainAbbr(slug) {
  const clean = String(slug ?? '')
    .replace(/-ar$/, '')
    .replace(/-/g, ' ')
    .trim();
  if (!clean) return '';
  if (clean.length <= 9) return clean.toUpperCase();
  return `${clean.slice(0, 8).toUpperCase()}.`;
}

/**
 * El color del punto que identifica a la cadena.
 *
 * Sale del slug y no de un índice: si dependiera del orden del ranking, el
 * color de Carrefour cambiaría cada vez que se mueve de puesto y dejaría de
 * identificar nada. La cadena que no tenga color asignado cae en gris — es
 * preferible a repetir uno y hacer que dos columnas parezcan la misma.
 */
const CHAIN_COLORS = ['carrefour', 'disco', 'dia', 'coto'];

export function chainColor(slug) {
  const key = String(slug ?? '').replace(/-ar$/, '');
  return CHAIN_COLORS.includes(key) ? `var(--chain-${key})` : 'var(--text-3)';
}

/**
 * Nombre presentable a partir del slug.
 *
 * Solo hace falta para una cadena que falló: sin respuesta no hay `ChainTotal`,
 * y por lo tanto tampoco `display_name`. Igual hay que nombrarla — «una cadena
 * no respondió» no le sirve a nadie parado en la puerta del súper.
 */
export function chainNameFromSlug(slug) {
  return String(slug ?? '')
    .replace(/-ar$/, '')
    .split('-')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

export const CONFIDENCE_LABEL = {
  pinned: 'fijado',
  exact_ean: 'exacto',
  name: 'aproximado',
};

export const CONFIDENCE_HINT = {
  pinned: 'Fijaste este producto por su código de barras: las dos cadenas comparan exactamente lo mismo.',
  exact_ean:
    'Las cadenas coincidieron en el mismo código de barras: es el mismo producto en las dos.',
  name: 'No hubo un código de barras en común. Cada cadena aportó su mejor coincidencia por nombre, así que pueden diferir en marca o gramaje.',
};

export const SCOPE_NOTE = {
  national: 'Precio nacional: no es el de tu sucursal.',
  channel: 'Precio del canal de venta, no de una sucursal puntual.',
  store: 'Precio de tu sucursal.',
};

/**
 * De cuándo son los precios que se están mostrando.
 *
 * El backend cachea las consultas a las cadenas hasta seis horas, así que
 * «cuándo pedimos la comparación» y «de cuándo son estos precios» dejaron de ser
 * lo mismo: la app puede haber preguntado recién y recibir precios de hace cinco
 * horas. Este es el segundo, que es el que importa para decidir dónde comprar.
 *
 * Del conjunto se toma el **menos** fresco, igual que hace el backend: un total
 * con dos precios de hace un minuto y uno de hace ocho horas está tan viejo como
 * ese último.
 */
export function worstFreshness(items) {
  const known = items.filter(Boolean);
  if (!known.length) return null;
  return known.reduce((worst, item) => {
    if (item.stale !== worst.stale) return item.stale ? item : worst;
    return item.age_seconds > worst.age_seconds ? item : worst;
  });
}

const PAYMENT_CONFIDENCE_LABEL = {
  measured: 'medido',
  estimated: 'estimado',
  mixed: 'parcialmente medido',
};

export function buildView(data) {
  if (!data) return null;

  const basket = data.basket ?? null;
  const coverage = new Map(
    (basket?.chains ?? []).map((chain) => [chain.chain_slug, chain]),
  );
  const bestOptionBySlug = new Map();
  for (const option of data.options ?? []) {
    const current = bestOptionBySlug.get(option.chain_slug);
    if (!current || option.final_total_cents < current.final_total_cents) {
      bestOptionBySlug.set(option.chain_slug, option);
    }
  }
  const failed = new Map(
    (basket?.errors ?? []).map((error) => [error.chain_slug, error.message]),
  );

  // `data.chains` ya viene ordenado por lo que terminás pagando.
  const chains = (data.chains ?? []).map((chain, index) => {
    const cover = coverage.get(chain.chain_slug);
    const option = bestOptionBySlug.get(chain.chain_slug) ?? null;
    return {
      slug: chain.chain_slug,
      name: chain.display_name,
      abbr: chainAbbr(chain.chain_slug),
      total: chain.best_final_total_cents,
      baseTotal: chain.base_total_cents,
      /* El ahorro de la tarjeta es `total_saving_cents`, que el backend mide
         restando dos simulaciones del checkout. Calcularlo como
         `base_total - final_total` daría bastante más —$5.244 en vez de $2.294
         en una canasta de prueba— porque el total base es la suma de precios de
         góndola y el checkout ya aplica por su cuenta promos que no dependen de
         con qué pagues. Ese número más grande es exactamente el que hace que
         una tarjeta parezca mejor de lo que es. */
      cardSaving: option?.total_saving_cents ?? 0,
      /* Lo que costaría sin ningún beneficio de medio de pago: es contra esto
         que se mide el ahorro, y por lo tanto es el precio que se tacha. */
      referenceTotal: option?.reference_total_cents ?? chain.base_total_cents,
      /* Lo que la cadena descuenta por su cuenta —2x1, segunda unidad al 50%—
         antes de que entre ninguna tarjeta. Es la diferencia entre sumar los
         precios de góndola y lo que contesta el checkout, y explica por qué la
         columna de precios no suma el total: sin esto, los dos números se
         contradicen en pantalla y no hay forma de saber cuál creer. */
      ownPromoSaving: Math.max(
        chain.base_total_cents - (option?.reference_total_cents ?? chain.base_total_cents),
        0,
      ),
      instrumentLabel: chain.best_instrument_label,
      simulated: chain.simulated,
      isCheapest: index === 0,
      /* Lo que esta cadena no resuelve: lo que no tiene y lo que tiene agotado.
         Van juntos porque para la compra son la misma cosa —ese producto lo
         vas a buscar a otro lado—, y es lo que el backend usa para rankear. */
      missing: [...(cover?.missing_lines ?? []), ...(cover?.unavailable_lines ?? [])],
      outOfStock: cover?.unavailable_lines ?? [],
      matched: cover?.matched_lines ?? 0,
      priceScope: cover?.price_scope ?? null,
      store: cover?.store ?? null,
      /* De cuándo son los precios de ESTA cadena. Va por cadena y no global
         porque el caso que importa es justamente el desparejo: Carrefour
         recién consultado y Disco servido de un precio de ayer porque su API
         no respondió. Un solo número escondería cuál es cuál. */
      freshness: cover?.freshness ?? null,
      /* La cadena no respondió y compite con su último precio conocido. */
      stalePrices: Boolean(cover?.freshness?.stale),
      error: failed.get(chain.chain_slug) ?? null,
      option,
      confidence: option?.confidence ?? null,
      confidenceLabel: option ? PAYMENT_CONFIDENCE_LABEL[option.confidence] : null,
      bankPromo: option?.bank_promo ?? null,
      providerPromotions: option?.provider_promotions ?? [],
      caveats: option?.caveats ?? [],
      /* Un reintegro ahorra lo mismo pero recién después: en la caja pagás todo.
         Confundirlos le arruina el presupuesto de la semana a quien lo usa. */
      outOfPocket: option?.out_of_pocket_today_cents ?? null,
      isReimbursement:
        option != null && option.out_of_pocket_today_cents > option.final_total_cents,
    };
  });

  // Una cadena que falló no produce total, así que no está en `data.chains`.
  // Se la nombra igual: su columna vacía es la diferencia entre "Disco es más
  // caro" y "de Disco no sabemos nada".
  const ranked = new Set(chains.map((chain) => chain.slug));
  const failedChains = (basket?.errors ?? [])
    .filter((error) => !ranked.has(error.chain_slug))
    .map((error) => ({
      slug: error.chain_slug,
      name: chainNameFromSlug(error.chain_slug),
      abbr: chainAbbr(error.chain_slug),
      message: error.message,
    }));

  const [best, runnerUp] = chains;
  const winnerHasGaps = Boolean(best?.missing.length);

  // Sin al menos dos cadenas en pie no hay comparación que hacer, solo un
  // precio. Y si alguna falló, el ranking está incompleto por definición.
  const comparable = chains.length >= 2 && !basket?.partial;

  // Las cadenas que compitieron con precios viejos porque su API no respondió.
  // Se nombran una por una: «algún precio puede estar desactualizado» obliga a
  // desconfiar de los cuatro números para cubrir uno.
  const stalePriceChains = chains.filter((chain) => chain.stalePrices);
  const freshness = worstFreshness(chains.map((chain) => chain.freshness));

  const lines = (basket?.lines ?? []).map((line, index) => {
    const byChain = new Map(line.matches.map((m) => [m.chain_slug, m]));
    return {
      id: `${line.query}-${index}`,
      query: line.query,
      quantity: line.quantity,
      confidence: line.confidence,
      ean: line.ean,
      comparable: line.comparable,
      cheapestChain: line.cheapest_chain,
      saving: line.saving_cents,
      missingIn: line.missing_in ?? [],
      byChain,
    };
  });

  return {
    chains,
    failedChains,
    lines,
    winner: best ?? null,
    runnerUp: runnerUp ?? null,
    /* Solo se afirma un ahorro cuando la comparación lo sostiene: dos cadenas
       en pie, ninguna caída, y la ganadora con la canasta completa. */
    savingVsRunnerUp:
      comparable && runnerUp && !winnerHasGaps
        ? runnerUp.total - best.total
        : null,
    winnerHasGaps,
    comparable,
    /* La antigüedad del precio menos fresco de toda la comparación. Es lo que
       va en la barra de contexto: reemplaza a «hace cuánto pedimos la
       comparación», que con la caché del backend ya no dice de cuándo son los
       precios. */
    freshness,
    stalePriceChains,
    /* Las que la franja de arriba tiene que nombrar: todas menos la ganadora.
       Si la ganadora sirvió precios viejos, eso lo dice el veredicto, pegado al
       número que se va a usar para decidir. Decirlo también en una franja sería
       el mismo aviso dos veces —el mismo criterio con el que el veredicto no
       repite la caída de una cadena que la franja ya nombró—. */
    staleBannerChains: stalePriceChains.filter(
      (chain) => chain.slug !== best?.slug,
    ),
    stale: Boolean(basket?.stale),
    partial: Boolean(basket?.partial),
    errors: basket?.errors ?? [],
    comparableCount: basket?.comparable_line_count ?? 0,
    totalCount: basket?.total_line_count ?? lines.length,
    notes: data.notes ?? [],
    unverifiedRules: data.unverified_rules ?? [],
    hasInstruments: (data.options ?? []).length > 0,
    onDate: data.on_date,
  };
}
