# Proyecto Full-Stack: Compras (FastAPI + React Vite)

Estructura moderna y modular para una aplicación web full-stack compuesta por un backend en Python con **FastAPI** y un frontend SPA dinámico en **React** compilado con **Vite**.

---

## ⚖️ Alcance y uso de datos de terceros

**Este es un proyecto personal y educativo. No tiene fines comerciales, no está
monetizado y no se ofrece como servicio a terceros.** Nació como una herramienta
para comparar el precio de una lista de compras propia y como trabajo de
facultad, y ese sigue siendo todo su alcance.

**De dónde salen los precios.** La app consulta las APIs públicas de catálogo que
los sitios de cada cadena usan desde el navegador: la API de VTEX de Carrefour,
Disco y Día, y la API de Constructor.io de Coto Digital. Son los mismos endpoints
que responde cualquier visitante del sitio, con las mismas claves públicas que
esos sitios le entregan al navegador. No se usa ninguna credencial privada, no se
accede a ningún endpoint autenticado y no se evade ningún control de acceso.

**Cómo se trata la carga sobre esos servicios.** El proyecto está escrito para
pesar lo menos posible sobre la infraestructura ajena, y eso es una decisión de
diseño explícita, no un accidente:

- Un limitador por cadena acota los requests en vuelo y les impone una
  separación mínima, para que nunca salgan en ráfaga
  (`app/services/http.py::AsyncRateLimiter`).
- Los reintentos van con backoff exponencial y jitter, y respetan el
  `Retry-After` cuando la cadena lo manda. Solo se reintenta lo transitorio:
  un 4xx nunca se martilla.
- Una caché con TTL responde las búsquedas repetidas sin volver a preguntar, y
  guarda el último precio conocido para servirlo —marcado como viejo— cuando una
  cadena no responde. En uso normal, la mayoría de las búsquedas no genera
  ningún request real.

**Qué NO es este proyecto.** No es un servicio de comparación de precios para uso
masivo, no redistribuye ni republica los catálogos ni los precios de ninguna
cadena, y no está pensado para alimentar a terceros con esos datos. Los precios
que muestra son para la decisión de compra de quien lo usa, en el momento en que
lo usa. Los archivos bajo `backend/tests/fixtures/` son respuestas grabadas de
esas APIs, incluidas únicamente para que los tests corran sin red.

Los términos de uso de estas cadenas restringen el uso comercial de sus datos sin
autorización previa. Este proyecto no hace uso comercial de ellos. Si sos titular
de alguno de estos servicios y querés que deje de consultarse el tuyo, abrí un
issue y lo saco.

---

## 📁 Estructura del Proyecto

```text
Compras/
├── backend/                  # Servidor de API en Python (FastAPI)
│   ├── app/
│   │   ├── main.py           # Instancia de FastAPI, CORS y rutas globales
│   │   ├── core/             # Configuración central (Pydantic Settings, .env)
│   │   ├── api/              # Endpoints modularizados por versiones (v1)
│   │   ├── models/           # Esquemas Pydantic y modelos de datos
│   │   ├── db/               # tables.py (esquema) y repositories.py (acceso)
│   │   └── services/         # Servicios y lógica de negocio
│   ├── alembic/              # Migraciones; la app corre `upgrade head` al arrancar
│   ├── compras.db            # SQLite (fuera de Git)
│   ├── requirements.txt      # Lista de librerías Python necesarias
│   ├── .env.example          # Plantilla de variables de entorno
│   └── .gitignore            # Exclusiones de Git para el backend
│
├── frontend/                 # Aplicación Cliente (React + Vite)
│   ├── src/
│   │   ├── styles/           # tokens.css (paleta, escala tipográfica) y base.css
│   │   ├── lib/              # api.js, money.js, storage.js, model.js
│   │   ├── hooks/            # lista, settings, comparación, tarjetas, domicilios, mapa
│   │   ├── components/       # Verdict, ChainStrip, LineTable, MapView, sheets
│   │   ├── App.jsx           # Pantalla única + estado de los paneles
│   │   └── main.jsx          # Punto de entrada React DOM
│   ├── vite.config.js        # Servidor Vite y proxy HTTP al backend
│   ├── .env.example          # VITE_API_URL / VITE_BACKEND_ORIGIN
│   ├── package.json          # Dependencias y scripts de Node.js
│   └── .gitignore            # Exclusiones de Git para el frontend
│
├── .gitignore                # Reglas globales de Git
└── README.md                 # Guía de instalación y uso
```

---

## 🚀 Guía de Inicio Rápido

### 1. Configurar y Ejecutar el Backend (FastAPI)

Navega a la carpeta del backend:
```bash
cd backend
```

Crear un entorno virtual de Python:
```bash
python3 -m venv venv
```

Activar el entorno virtual:
- En Linux / macOS:
  ```bash
  source venv/bin/activate
  ```
- En Windows (CMD / PowerShell):
  ```bash
  venv\Scripts\activate
  ```

Instalar dependencias:
```bash
pip install -r requirements.txt
```

Iniciar el servidor de desarrollo:
```bash
uvicorn app.main:app --reload --port 8000
```

- **API Base:** `http://localhost:8000`
- **Documentación Interactiva Swagger:** `http://localhost:8000/docs`
- **Documentación ReDoc:** `http://localhost:8000/redoc`

---

### 2. Configurar y Ejecutar el Frontend (React + Vite)

Abre otra terminal y navega a la carpeta del frontend:
```bash
cd frontend
```

Instalar las dependencias de Node.js:
```bash
npm install
```

Iniciar el servidor de desarrollo Vite:
```bash
npm run dev            # solo esta máquina
npm run dev -- --host  # también desde el celular, por la red local
```

- **App Frontend:** `http://localhost:5173`

Sin configurar nada, el frontend pega a `/api/v1` y Vite lo proxea a
`http://127.0.0.1:8000`. Eso alcanza incluso desde el celular con `--host`: el
teléfono le pega al Vite de la notebook y ese reenvía al backend. Para apuntar a
otro backend, copiar `.env.example` a `.env.local` (`VITE_API_URL` para el
cliente, `VITE_BACKEND_ORIGIN` para el proxy).

---

---

## 🛒 Proveedores de precios (Carrefour + Disco + Coto)

Carrefour y Disco corren sobre **VTEX**, así que un único cliente parametrizado
cubre las dos cadenas. Agregar Jumbo, Vea u otro VTEX argentino es agregar una
constante en `app/services/providers/vtex/stores.py`.

**Coto no es VTEX**: su catálogo lo sirve la API pública de **Constructor.io**
(`ac.cnstrc.com`), el motor de búsqueda de Coto Digital. Entró sin tocar el
contrato — `PriceProvider` es un `Protocol` y se cumple por estructura — y ese
era justamente el punto del diseño.

```
app/services/providers/
├── base.py              # Protocol PriceProvider (se cumple por estructura)
├── common.py            # normalize_ean / to_cents: no son de ninguna cadena
├── registry.py          # slug -> proveedor, vive en el lifespan de la app
├── vtex/
│   ├── config.py        # VTEXStoreConfig: la costura genérico/específico
│   ├── stores.py        # CARREFOUR_AR, DISCO_AR
│   ├── client.py        # protocolo VTEX: paginación, topes, encoding
│   ├── quirks.py        # rarezas de serialización
│   ├── mapper.py        # crudo -> modelos canónicos
│   └── provider.py      # implementación de PriceProvider
└── coto/
    ├── config.py        # CotoConfig: api key, sucursal de referencia, cordura
    ├── client.py        # Constructor.io: /search/{term} y /browse/group_id
    ├── mapper.py        # crudo -> modelos canónicos (funciones puras)
    └── provider.py      # implementación de PriceProvider
```

### Lo que Coto hace distinto

- **Da precio por sucursal de verdad.** Cada producto trae el precio de las ~34
  sucursales en `data.price`, así que es la primera cadena soportada que puede
  devolver `PriceScope.STORE`. La contracara: Coto no publica qué sucursal sirve
  a un código postal, así que `find_stores` devuelve vacío y hay que saber el
  número de sucursal para pedirlo.
- **No tiene checkout consultable.** Sin simulación no hay precio autoritativo:
  `simulate_basket` devuelve `None` y `verify_prices` devuelve `[]`, igual que
  Disco. En `/basket/best-payment` eso sale como `confidence: estimated` con el
  caveat correspondiente, en lugar de un total inventado.

### Endpoints

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/v1/chains` | cadenas soportadas y si dan precio por sucursal |
| GET | `/api/v1/stores?postal_code=1425` | sucursales cercanas |
| GET | `/api/v1/search?q=leche` | busca en todas las cadenas en paralelo |
| GET | `/api/v1/compare?q=coca cola` | mismo EAN en más de una cadena, ordenado por ahorro |
| GET | `/api/v1/products/{ean}/history?days=90` | evolución del precio |
| POST | `/api/v1/basket/compare` | **tu lista de compra entera: cuánto sale en cada cadena** |
| POST | `/api/v1/basket/verify` | confirma una canasta contra el checkout |
| POST | `/api/v1/basket/best-payment` | lo mismo pero con tarjetas: cadena × medio de pago, más el detalle en `basket` |
| GET / PUT | `/api/v1/profile` | CP, canal, tema y tus datos |
| GET / POST | `/api/v1/addresses` | domicilios guardados (`PATCH` y `DELETE` por id) |
| GET | `/api/v1/shopping-lists/default` | la lista con la que trabaja la pantalla; la crea si no hay |
| PUT | `/api/v1/shopping-lists/{id}` | guarda la lista entera |
| GET | `/api/v1/store-locations?postal_code=1425` | sucursal más cercana de cada cadena, para el mapa |
| GET | `/api/v1/geocode?address=Gallo 250` | direcciones que coinciden, **para elegir una** |
| GET / POST | `/api/v1/custom-stores` | sucursales cargadas a mano (`PATCH` y `DELETE` por id) |

### Comparar una lista de compra

`POST /api/v1/basket/compare` recibe la lista en texto libre y devuelve el total
por cadena. Dos decisiones de diseño que conviene entender antes de leer los
números:

- **El ranking sale de `comparable_total_cents`**, no del total crudo. Solo suma
  las líneas que *todas* las cadenas tienen. Si a Disco le faltan la yerba y el
  pan, su total completo va a ser menor por eso, no por ser más barato — es el
  error clásico de este tipo de app.
- **Cada línea declara su `confidence`.** `exact_ean` significa que se comparó el
  mismo producto (mismo EAN) en todas las cadenas. `name` significa que no había
  EAN común y cada cadena aportó su mejor coincidencia: pueden diferir en marca o
  gramaje, así que esa línea es orientativa.

Para elegir el producto de cada línea **no se ordena por precio**: para "leche" lo
más barato suele ser una chocolatada de 200 ml. Se respeta el orden de relevancia
de la cadena y se ancla por EAN común cuando existe.

```bash
curl -s localhost:8000/api/v1/basket/compare -H 'content-type: application/json' -d '{
  "lines": [
    {"query": "leche entera 1L", "quantity": 3},
    {"query": "yerba mate 1kg"},
    {"query": "coca cola 2.25", "quantity": 2, "ean": "7790895005312"}
  ],
  "postal_code": "1425"
}'
```

`ean` en una línea fija el producto exacto y saltea la heurística. `verify: true`
confirma el total contra el checkout, que aplica las promociones reales.

### Caché de precios

Cada búsqueda contra una cadena se guarda en `price_search_cache` y se reusa
durante seis horas. Sin esto, comparar una lista de 15 líneas contra 4 cadenas
son 60 requests, y repetirla dos minutos después son 60 más.

La capa es un `CachedProvider` que **envuelve al proveedor** en el registry
(`services/providers/cache.py`). Va ahí y no dentro de `search_all` porque los
caminos que piden precios son varios —`/search`, `/compare` y el fan-out de
canastas— y ninguno pasa por el otro; envolviendo el proveedor, los tres la
heredan, y las cadenas que vengan quedan cubiertas por construcción.

**Cada precio dice de cuándo es.** Toda oferta trae un `freshness` con
`fetched_at`, `age_seconds`, `from_cache` y `stale`, y `/search` además resume
por cadena en `sources` — que es lo que necesita el frontend para poner
"actualizado hace 3 horas" sin recorrer los resultados. En la canasta, cada
`ChainTotal` trae el `freshness` de su precio **menos** fresco: un total con dos
precios de hace un minuto y uno de hace ocho horas está tan desactualizado como
ese último.

**Si una cadena no responde, no se pierde.** Vencido el TTL, si la API falla se
sirve igual el último precio conocido marcado `stale: true`, hasta un techo de 7
días. Una cadena caída sigue compitiendo en el ranking con esa marca puesta:
sacarla también es un resultado equivocado, y peor, porque no se ve. Sin nada
guardado el fallo se propaga como antes y la cadena queda fuera.

**Lo que nunca se cachea:** `verify_prices` y `simulate_basket`. Son el precio
confirmado contra el checkout, el número con el que alguien decide dónde
comprar; uno de hace seis horas es una contradicción.

`?fresh=true` en `/search` y `/compare`, o `"fresh": true` en el body de
`/basket/compare`, ignoran la caché y consultan en vivo — pero conservan el
respaldo: pedir datos frescos es pedir que se intente ir a buscarlos, no pedir
que la cadena desaparezca si falla.

```bash
PRICE_CACHE_ENABLED=true     # false = siempre en vivo, sin respaldo
PRICE_CACHE_TTL_S=21600      # 6 h
PRICE_CACHE_STALE_MAX_S=604800  # 7 días de techo para el respaldo
```

Seis horas y no doce: las promos bancarias son **por día de la semana** (ver
`data/payment_promos.yaml`), así que una ventana más larga puede servir a las 8
del jueves un precio capturado el miércoles a la tarde, con una promo que ya no
corre. Con seis horas la ventana nunca cruza dos días promocionales.

### Lo que hay que saber antes de tocarlo

Estas son restricciones medidas contra las APIs reales, no supuestos:

- **`catalog_system` es la fuente primaria.** Intelligent Search pierde catálogo
  en silencio: en Carrefour devuelve 0 resultados para "arroz", "aceite" y
  "azúcar", donde legacy devuelve entre 500 y 750.
- **Los espacios van como `%20`, nunca como `+`.** El WAF responde
  `400 Bad Request! Scripts are not allowed!`. httpx codifica con `+` por
  defecto, por eso `client.py` arma la query a mano.
- **Regionalización distinta por cadena.** Carrefour usa `sc` (1, 3 y 5 activos,
  y sí mueve precios); Disco no expone ninguna y sus precios son nacionales.

### Cómo se resuelve el precio

Cada consulta intenta el nivel más específico posible y **cae al siguiente en vez
de fallar** — pedir un canal de Carrefour no puede dejarte sin los precios de
Disco. `Offer.price_scope` declara el nivel que efectivamente se alcanzó:

| Nivel | Cuándo | Hoy |
|---|---|---|
| `store` | la cadena sabe qué canal usa esa sucursal | ninguna lo expone |
| `channel` | se pidió un `sales_channel` que la cadena tiene activo | Carrefour: 1, 3, 5 |
| `national` | canal por defecto — el piso al que se cae siempre | Disco siempre |

La lógica vive en `VTEXStoreConfig.resolve_price_scope`. El día que se pueda
medir qué canal usa cada sucursal, se llena `Store.sales_channel` en
`map_regions_to_stores` y el nivel `store` empieza a funcionar solo.

Los canales están en `GET /api/v1/chains`, y se piden con `sales_channel` en
`/search`, `/compare` y `/basket/compare`.
- **Topes de paginación**: 50 items por página y offset máximo 2500. Una
  categoría grande necesita subdividirse; el cliente avisa cuando trunca.
- **`ListPrice` no es confiable** (Disco publicó 252066 contra un precio de
  3050). Se usa `PriceWithoutDiscount` y `ListPrice` solo si pasa un control.
- **VTEX devuelve errores como JSON string suelto** (`"sc is inactive"`), a veces
  con HTTP 200. Sin la guarda, el mapper revienta lejos de la causa.
- **El checkout de Disco rechaza su propio catálogo** (`ORD027`), incluso con el
  `sellerId` que Disco publica. Por eso `supports_simulation=False`: sin eso, la
  confirmación de canasta devolvía un total de $0 que parecía una ganga.
- **El `sc` es la única palanca de precio, y no se puede atribuir a una
  sucursal.** `/regions` identifica bien las sucursales (Warnes vs Córdoba Colón)
  pero no dice en qué canal compran, y la simulación devuelve el mismo precio
  para todos los sellers (`1`, `carrefourar0026`, `carrefourar0009`). Por eso
  `map_regions_to_stores` deja `sales_channel=None` y la resolución cae a
  nacional: etiquetar ese precio como "de tu sucursal" hacía que dos códigos
  postales distintos dieran el mismo total con etiqueta de local.
- **Solo el canal 1 de Carrefour surte de verdad.** El 3 y el 5 publican precios
  distintos (Leche Protein: $2.249 vs $2.999) pero casi sin stock: medido sobre 6
  términos, el 1 dio 20/20 ofertas disponibles siempre, el 3 es errático (20/20
  en "leche", 0/20 en "arroz") y el 5 dio 0/20 en todos. Se pueden consultar —el
  precio es información real— pero rara vez producen ofertas comprables, y lo que
  no tiene stock nunca entra en un total comparable.

### Rarezas medidas de Coto (Constructor.io)

- **`formatPrice` NO es el precio con descuento: es el precio por unidad de
  medida.** Es la trampa más cara de esta API, porque el nombre sugiere lo
  contrario y el número es plausible. Es `listPrice` dividido por el tamaño del
  envase declarado en `product_format`:

  | Producto | `listPrice` | `formatPrice` | qué es |
  |---|---|---|---|
  | Aceite oliva 500 ml | 13.799 | 27.598 | 13.799 / 0,5 L |
  | Aceite girasol 1,5 L | 6.530 | 4.353,34 | 6.530 / 1,5 L |
  | Fritolim 120 g | 4.900 | 40.833,33 | 4.900 / 0,120 kg |

  Tomarlo como precio de venta publicaría el Fritolim a **8 veces** su precio y
  el aceite de 1,5 L a **dos tercios** del suyo —que es peor, porque no se nota:
  simplemente hace que Coto gane comparaciones que no gana—. El precio va a
  `price_cents` desde `listPrice`, y `formatPrice` alimenta
  `price_per_unit_cents`, que es justo el campo que hacía falta para comparar
  1 L contra 900 ml (en VTEX hay que derivarlo del `unitMultiplier`).

- **`discounts` mezcla dos cosas que no se pueden tratar igual.** Con
  `takingText` ("Llevando 2") el `discountPrice` es el promedio *llevando dos*:
  "50% 2da" da 0,75 del precio y "2x1" da 0,50. Aplicarlo a una unidad suelta
  inventa un descuento que en la caja no existe. Sin `takingText` ("25%Dto") sí
  se paga por una unidad, y ahí `discountPrice` es el precio y `listPrice` pasa a
  ser el tachado. Las promos por cantidad se declaran en `promotions` con su
  `minimum_quantity`, sin tocar el precio.

- **Sin match léxico devuelve cualquier cosa con HTTP 200.** `zzzqqxnotaproduct`
  devuelve 20 resultados encabezados por "Remera Niño/A Estampada Skate": es el
  fallback por embeddings de Constructor.io. Se distinguen porque vienen con
  `matched_terms` vacío, y se descartan. Sin ese filtro, buscar "leche" mezclaba
  ropa de Coto con la leche de Carrefour.

- **La sucursal 133 publica `formatPrice` roto** ($29,05 para una leche de 1 L de
  $2.495; mediana de cociente 0,015 contra 2,0 en las otras 34). Su `listPrice`
  sí sirve, así que se descarta el precio por unidad y se conserva la oferta.

- **El catálogo arrastra fichas de productos discontinuados** con precios
  congelados ("Aceite Girasol VICENTIN 1.5 Ltr" a $335,38). Se reconocen por
  `store_availability` vacío — y ahí la fórmula de `formatPrice` tampoco cierra,
  lo que confirma que son datos muertos. Se mapean igual, con `available=False`,
  y la regla general los deja fuera de cualquier total comparable.

### Tests

```bash
cd backend
./venv/bin/python -m pytest -q          # 294 tests, sin red (respx + fixtures reales)
./venv/bin/python -m pytest -m live -q  # 35 tests contra las APIs reales
```

Las fixtures de Coto se regraban con
`./venv/bin/python scripts/record_coto_fixtures.py`, que elige los casos por el
rasgo que ejercitan (promo por cantidad, descuento directo, sucursal 133 rota) y
no por venir primero, así siguen probando lo mismo aunque Coto reordene.

Los `live` son el **detector de deriva**: no prueban nuestro código, avisan si
Carrefour, Disco o Coto cambiaron algo. Conviene correrlos periódicamente; si
fallan, revisar la config antes de que el comparador empiece a mostrar precios
equivocados. Para regrabar las fixtures:
`./venv/bin/python scripts/record_fixtures.py`.

### Dónde queda cada súper (el mapa)

Ninguna cadena publica las coordenadas junto con los precios, y cada una lo
resuelve distinto. Medido contra las cuatro:

| cadena | ubicaciones | fuente | coordenadas |
|---|---|---|---|
| Carrefour | 607 | `GET /api/checkout/pub/pickup-points` (VTEX) | las publica |
| Día | 776 | `GET /api/checkout/pub/pickup-points` (VTEX) | las publica |
| Coto | 122 (62 ubicables) | tabla HTML en `coto.com.ar/sucursales` | no: hay que geocodificar |
| Disco | 0 | no publica nada (`pickup-points` devuelve `total: 0`) | — |

Se cargan a mano, no en el arranque:

```bash
cd backend
./venv/bin/python scripts/refresh_store_locations.py
./venv/bin/python scripts/refresh_store_locations.py --skip-geocoding    # sin geocodificar nada
./venv/bin/python scripts/refresh_store_locations.py --geocoder nominatim  # el geocodificador viejo
```

Cuatro cosas medidas que conviene saber antes de tocar esto:

- **VTEX devuelve `[longitud, latitud]`**, al revés de como se escriben. El par
  invertido sigue siendo dos números válidos, así que nada falla: simplemente
  Buenos Aires aparece en China. Hay un test que lo fija.
- **Los resultados vienen ordenados por distancia al punto consultado**, así que
  barrer desde un solo lugar no cubre el país: desde CABA faltan las sucursales
  de Córdoba y Mar del Plata. Por eso se siembra con ocho coordenadas.
- **Los números de sucursal de Coto no cubren los del array de precios**: la
  sucursal de referencia (`200`) no está en la tabla de `/sucursales`. Alcanza
  para poner un marker; no para atarle un precio a una sucursal.
- **Coto se geocodifica con Georef**, la API del Estado argentino
  (`apis.datos.gob.ar`), gratis y sin API key. Reemplazó a Nominatim, que sigue
  disponible con `--geocoder nominatim`, por dos motivos medidos: **llega** desde
  redes que tienen bloqueado OSM (ver más abajo) y **acierta más** — «Gallo 250,
  CABA» lo pone en Comuna 3, que es donde está, mientras Nominatim y Photon lo
  mandan a Sarmiento y a Banfield con total confianza. Todo resultado se cachea
  en `geocode_cache`, los fracasos incluidos.
- **De las 122 sucursales de Coto se ubican 62, y eso es deliberado.** Las 58 de
  CABA salen bien (verificado una por una por geocodificación inversa: todas caen
  en su barrio) y las 4 de Rosario y Santa Fe también, después de sacar la ciudad
  que Coto mete adentro del texto de la dirección —«Urquiza 1644 (Rosario)»— que
  sin separar resuelve a 200 km. **Las 43 del conurbano no se geocodifican**: Coto
  publica «Zona Norte/Sur/Oeste», que no es una unidad administrativa que ningún
  callejero conozca, y con eso lo mejor que se logró fue acotar por provincia y
  validar contra una caja del AMBA — entran 22 y **ocho caen en el partido
  equivocado** (Munro en Berazategui, Castelar en Vicente López, José C. Paz en
  San Fernando). No hay filtro que invente el dato que falta, y un marker en el
  partido equivocado es peor que ningún marker, así que esas quedan para la carga
  manual. Todo esto está fijado en tests.

El mapa **no calcula nada**: consume el mismo `view` que la tabla y usa sus
`isCheapest` y `total` tal como vienen. Si recalculara cuál conviene, podría
contradecir a la tabla que está al lado y no habría forma de saber cuál creer.

Los tiles son los de OSM, gratis y sin API key. Para el tema oscuro se probó el
basemap oscuro de CARTO y **hoy exige API key**: los tiles cargan, pero con «API
KEY REQUIRED» estampado encima. Así que el modo oscuro invierte los tiles claros
por CSS, sobre `.leaflet-tile-pane` y no sobre el mapa entero, para que los
markers conserven sus colores. No es una conversión fiel —las calles quedan
asalmonadas y los parques marrones—, pero se lee bien y no cuesta una credencial.

**El proveedor de tiles tiene plan B, y hace falta.** `tile.openstreetmap.org`
resuelve a cuatro IPs de Fastly (`151.101.*.91`) que algunos ISP argentinos
bloquean por rango; desde esas conexiones los tiles nunca llegan y Leaflet, que
los mantiene en `visibility:hidden` hasta que cargan, deja un rectángulo vacío
idéntico a un bug de la app. Está verificado que el bloqueo es por IP y no por
dominio: pedirle a `www.openstreetmap.org` —que responde 200— por una de esas IPs
también corta. Por eso `MapView` prueba en orden OSM → mirror de OSM France →
Esri, y avisa en pantalla cuál terminó usando. El corte es **por tiempo** y no por
evento `tileerror`: con un bloqueo a nivel IP la conexión no se rechaza, se
cuelga, y el error puede tardar más de un minuto en llegar o no llegar nunca.

> **El mismo bloqueo alcanza a Nominatim**, que vive en esas IPs: desde una red
> bloqueada no contesta nunca. Es lo que motivó pasar la geocodificación a Georef,
> que está en otra infraestructura y responde en ~0,1 s. Photon
> (`photon.komoot.io`) también llega y tampoco pide key, pero se descartó por
> puntería: «Gallo 250, Capital Federal» le cae en Sarmiento.

Solo Carrefour ata una sucursal a su precio; Disco y Día publican precio
nacional y Coto no resuelve sucursal por código postal. En esos casos el marker
contesta *dónde queda el súper* y el popup aclara, con la misma nota que usa la
tabla, que el precio no es el de ese local.

### Desde dónde se mide «la más cercana»

El centro del mapa sale de lo más preciso que haya, en este orden:

1. Las coordenadas del pedido (`?latitude=&longitude=`), si vienen.
2. **Tu domicilio ubicado**, cargado en «Mis domicilios».
3. El promedio de las sucursales que ya están en tu código postal.
4. Geocodificar el código postal.

El salto que importa es del 3 al 2. Un CP de CABA abarca un área donde entran
tres sucursales de la misma cadena, así que «la más cercana» calculada desde el
centro del CP puede ser una que te queda a quince cuadras de la que tenés
enfrente. Medido: con CP 1425 la Carrefour más cercana es Güemes 4161; cargando
una dirección en Almagro pasa a ser Corrientes 3401.

Ubicar un domicilio **no cambia ningún precio**: esos los sigue resolviendo el CP
de Ajustes. Es a propósito — guardar la dirección del trabajo no tiene por qué
mover los totales — y por eso `Address.postal_code` y el CP del perfil son campos
distintos.

La dirección se ubica buscándola con Georef, que **devuelve varias opciones y no
elige**: «Av. Belgrano 950» existe en ocho partidos del país y todos le parecen
igual de plausibles. Quien carga su casa sabe cuál era. Si el buscador no
encuentra la dirección o no se puede llegar a él, se pegan las coordenadas a
mano.

### Sucursales cargadas a mano

`custom_stores` guarda los súper que agregás vos, desde «Mis sucursales» o desde
el propio mapa. Resuelve dos cosas que la carga automática no puede: las 43
sucursales del conurbano de Coto que quedan sin geocodificar, y corregir
cualquiera que haya quedado mal ubicada.

Tres decisiones, con su motivo:

- **Tabla aparte y no una fila en `stores`.** `stores` la escriben los
  proveedores: `sync_stores` hace upsert por `(chain_id, external_id)` y el
  script de refresco matchea por nombre normalizado, así que una fila cargada a
  mano ahí puede quedar pisada por una corrida del script. Además una fila de
  `stores` es candidata a resolver precios, y estas no lo son: no tienen
  `sellerId` ni número de sucursal en el catálogo de la cadena.
- **La manual le gana a la automática de la misma cadena**, aunque quede más
  lejos. La cargaste justamente porque la automática está mal ubicada o no
  existe; que le gane por doscientos metros calculados sobre una coordenada que
  ya sabemos dudosa deshace el trabajo que hiciste.
- **Solo cadenas que la app compara.** Un Chango Más cargado a mano dibujaría un
  marker sin precio al lado de markers con precio, y dos markers que se ven igual
  y significan cosas distintas confunden más de lo que ayudan.

No cambia ningún precio: el total del marker sigue siendo el de la cadena,
calculado igual que en la tabla.

### Base de datos

**SQLite**, en `backend/compras.db`. Guarda el histórico de precios, el catálogo
de cadenas y sucursales, tus medios de pago, tu perfil, tus domicilios y tus
listas. Para un solo usuario en una máquina es lo correcto: Postgres pediría un
contenedor, un servicio corriendo y backups a cambio de nada, mientras que acá
el backup es copiar un archivo.

El motor está afinado en `app/core/db.py`: WAL para poder leer mientras un crawl
escribe, y `busy_timeout` para que un lector no se caiga con *database is
locked*.

El esquema lo maneja **Alembic**, y la app corre `alembic upgrade head` sola al
arrancar. `Base.metadata.create_all` quedó solo para los tests: crea las tablas
que faltan pero nunca altera una que ya existe, así que agregar una columna
pasaría en silencio y reventaría en el primer query.

```bash
cd backend
./venv/bin/alembic revision --autogenerate -m "qué cambió"   # tras tocar tables.py
./venv/bin/alembic upgrade head                              # aplicar
./venv/bin/alembic check                                     # ¿el esquema quedó al día?
```

El código no usa tipos atados a un dialecto, así que mudar la base a un servidor
es cambiar una variable —no hay código que tocar:

```bash
DATABASE_URL="postgresql+asyncpg://usuario:password@host/compras"   # + asyncpg
```

---

## 🔐 Cuentas y sesiones

Todo endpoint pide sesión salvo `/health` y `/auth/*`. Los datos están separados
por usuario: cada domicilio, tarjeta, lista y sucursal propia cuelga de un
`user_id`, y los repositorios lo exigen en el constructor —`AddressRepository(session, user.id)`—
en vez de aceptarlo como parámetro opcional. Es a propósito: con un default,
olvidarse de pasar el usuario en un endpoint devolvería los datos de otra
persona en silencio; así, directamente no compila.

### Cómo entra la primera vez

La migración `c3a71f4b92de` crea tu cuenta a partir del email que tuvieras en el
perfil y le reserva el `user_id = 1`, que es el que ya tenían todos tus datos. No
se migra ninguna fila. Pero la crea **sin contraseña**, porque la alternativa era
dejar una escrita en un archivo del repo:

```bash
cd backend
./venv/bin/python scripts/set_password.py                    # ver las cuentas
./venv/bin/python scripts/set_password.py tu@email.com       # asignar la contraseña
```

En una base nueva no se crea ninguna cuenta: te registrás desde la app y sos el
usuario 1.

### Cómo funciona

**Sesiones opacas, no JWT.** El token son 256 bits de `secrets` y en la base se
guarda solo su SHA-256, igual que una contraseña. Se eligió sobre JWT porque acá
la revocación importa más que evitar una consulta: un JWT vale hasta que expira,
así que "cerrar sesión" no cierra nada y cambiar la contraseña no echa a quien te
robó el token. El costo es un `SELECT` por request sobre un índice único.

**La cookie es `HttpOnly`**, así que el JavaScript de la página no puede leerla y
un XSS no se lleva la sesión — que es exactamente lo que sí puede hacer con un
token en `localStorage`. `SameSite=lax` es lo que bloquea el CSRF, sin necesidad
de tokens anti-CSRF.

**Contraseñas con Argon2id**, con largo mínimo y sin reglas de "una mayúscula y
un símbolo": empujan a `Password1!` y el NIST las desaconseja desde 2017.

**El login no dice si una cuenta existe.** Email inexistente y contraseña
equivocada dan el mismo 401 y tardan lo mismo, porque el caso inexistente igual
verifica contra un hash de descarte. Sin eso, el formulario es un buscador de
cuentas. Después de 10 fallos, esa IP recibe 429.

### Roles

La columna `role` existe y `require_role("admin")` está implementado y probado,
pero hoy ningún endpoint lo usa: todas las cuentas son `user`. Separar admin es
agregar la dependencia a una ruta, sin migrar el esquema ni invalidar sesiones.

### Antes de exponerlo en internet

- **`COOKIE_SECURE=true`** (default). En desarrollo contra `http://localhost`
  ponelo en `false` o el navegador descarta la cookie: el login devuelve 200 y la
  request siguiente da 401. Es el error más confuso de la configuración.
- **`CORS_ORIGINS` con los orígenes reales.** Nunca `*`: va junto a
  `allow_credentials=True`.
- **Servir el frontend del mismo sitio que la API.** Si van en dominios
  distintos hace falta `COOKIE_SAMESITE=none`, que apaga la protección CSRF que
  hoy es gratis.
- **HTTPS de punta a punta**, o la cookie `Secure` nunca llega.
- El rate limiter del login **vive en memoria del proceso**: con varios workers
  de uvicorn cada uno cuenta por su lado y el techo real se multiplica. Con más
  de uno, hay que moverlo a Redis.

> **Sobre tus datos.** La base guarda DNI, email, teléfono, domicilios con
> coordenadas y BIN de tarjetas. El login los protege del acceso por red, pero no
> del archivo: `compras.db` y cualquier copia suya están fuera de Git y tienen que
> seguir estándolo.

---

## 💡 Características Clave

- **Separación de Responsabilidades:** Backend y Frontend desacoplados para despliegue independiente o con Docker.
- **Configuración de CORS y Proxy:** El frontend incluye configuración de proxy en `vite.config.js` y el backend en `app/main.py` para evitar problemas de dominios cruzados.
- **Tipado de Esquemas:** Validaciones automáticas de datos mediante **Pydantic** en la API.

---

## 📱 El frontend

Una sola pantalla, pensada para abrirse desde el celular con la lista en la mano:
el veredicto —dónde comprar y cuánto sale— ocupa el primer viewport, y abajo está
el detalle producto por producto que lo respalda.

**Toda la pantalla sale de una request** a `POST /basket/best-payment`, que además
de los totales por cadena devuelve el detalle línea por línea en `basket`. Pedir
ese detalle aparte a `/basket/compare` duplicaría el fan-out contra Carrefour y
Disco y devolvería precios de otro instante, que podrían no cerrar con los
totales mostrados.

Lo que el diseño se ocupa de no esconder, porque es donde este tipo de app
engaña más fácil:

- **Cobertura al lado de cada total.** El ranking sale de `total_cents`, así que
  una cadena a la que le faltan productos da un total más bajo por faltarle
  productos. Cuando le falta algo a la ganadora, se deja de afirmar el ahorro.
- **La columna de precios no suma el total, y se explica.** Son precios de
  góndola; el checkout aplica después sus propias promos. El desglose al pie va
  de la suma de góndola al total, separando lo que descuenta la cadena de lo que
  descuenta la tarjeta.
- **El ahorro de la tarjeta es `total_saving_cents`**, que el backend mide
  restando dos simulaciones. Calcularlo como `base − final` da bastante más
  —$5.244 contra $2.294 en una canasta de prueba— y hace parecer mejor a la
  tarjeta de lo que es.
- **Cada línea declara su confianza** (`exacto` / `aproximado`), y el precio es
  nacional salvo que la cadena diga lo contrario.

La lista de compras y los ajustes viven en el backend, con `localStorage` de
caché. El orden importa: el primer render sale de la caché, **sincrónicamente**,
y recién después se reconcilia con la API. Si arrancara vacío y se llenara al
contestar el backend, el CP cambiaría después del primer render y eso dispararía
una segunda comparación contra los supermercados en cada arranque.

Esa misma caché es lo que mantiene la app usable con el backend apagado. El
último resultado también se cachea, así que la app abre con los números puestos
mientras refresca por atrás, y un error de red **no borra** los precios que
estabas leyendo.

### De cuándo son los precios que ves

Hay **dos** cachés y no son la misma: esta guarda el último *resultado* en
`localStorage`, y la del backend guarda los *precios* de cada cadena por seis
horas. Por eso «hace cuánto pedimos la comparación» dejó de contestar «de cuándo
son estos precios»: la app puede haber preguntado recién y recibir precios de
hace cinco horas.

Lo que se muestra es lo segundo. La antigüedad de la barra de contexto sale del
`freshness` que manda el backend —del precio **menos** fresco de la
comparación—, no del reloj del navegador.

Cuando una cadena no responde y el backend sirve su último precio conocido, se
dice en tres lugares, cada uno con su alcance:

| Dónde | Qué dice |
|---|---|
| Barra de contexto | la edad en ámbar, igual que cuando la lista cambió |
| Tarjeta de la cadena | un chip con la edad de *esa* cadena, al lado de su total |
| Franja o veredicto | la cadena por su nombre y desde cuándo son sus precios |

La franja nombra a todas **menos a la ganadora**: de esa habla el veredicto,
pegado al número que se va a usar para decidir. Decirlo en los dos lados sería
el mismo aviso dos veces.

El botón ⟳ manda `fresh: true` y consulta en vivo, salteando la caché del
backend. Los demás refrescos —la lista cambió, se guardó una tarjeta, se cambió
el CP— no la saltean: lo que cambió es de este lado, y los precios guardados
siguen valiendo.

`dev-preview.html?state=stale` monta la pantalla con una cadena servida vencida,
para poder mirar los tres avisos sin esperar a que se caiga un supermercado.

```bash
cd frontend
npm run lint    # eslint, sin warnings
npm run build   # bundle de producción
```
