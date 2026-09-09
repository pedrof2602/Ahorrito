from fastapi import APIRouter, Depends, status
from typing import List
from datetime import datetime

from app.api.v1.auth import router as auth_router
from app.api.v1.locations import router as locations_router
from app.api.v1.payments import router as payments_router
from app.api.v1.prices import router as prices_router
from app.api.v1.profile import router as profile_router
from app.api.v1.voz import router as voz_router
from app.core.auth import current_user
from app.models.item import ItemCreate, ItemResponse

router = APIRouter()

# Registro y login. Es el único router sin `current_user`, por razones obvias.
router.include_router(auth_router)

# --- de acá para abajo, todo pide sesión ------------------------------------
#
# La dependencia se aplica al router y no endpoint por endpoint por dos motivos.
# El primero es que así el default es "cerrado": una ruta nueva queda protegida
# por omisión, y abrirla es un acto deliberado. Al revés —proteger de a una— el
# olvido se paga con un endpoint público que nadie nota.
#
# El segundo es que buena parte de estos endpoints no necesitan saber *quién*
# sos, solo que seas alguien: `/search` y `/compare` no tocan datos de usuario,
# pero cada llamada dispara un fan-out contra Carrefour, Disco, Día y Coto.
# Abiertos en un deploy público son un proxy gratis contra los supermercados,
# pagado con la cuota y la reputación de IP de este server. Pedirles `user:
# CurrentUser` en la firma solo para descartarlo sería un parámetro sin usar en
# cada una; los que sí necesitan la identidad la piden igual, y FastAPI cachea
# la dependencia por request, así que resolverla dos veces no cuesta una segunda
# consulta a la base.
_authenticated = [Depends(current_user)]

# Precios y comparación entre supermercados.
router.include_router(prices_router, tags=["precios"], dependencies=_authenticated)

# Medios de pago y promociones: el precio real según con qué pagues.
router.include_router(payments_router, dependencies=_authenticated)

# Perfil, domicilios y listas: lo que antes vivía en el localStorage.
router.include_router(profile_router, dependencies=_authenticated)

# Ubicaciones de sucursales: el "dónde" que la tabla de precios no contesta.
router.include_router(locations_router, dependencies=_authenticated)

# La lista manejada desde afuera: el Atajo de Siri del iPhone.
#
# **Sin `_authenticated`, y es la única excepción a la regla de arriba.** Un
# atajo del sistema operativo no tiene navegador, ni sesión, ni cookie: se
# autentica con un token personal que lleva en el header, y `voz.py` lo resuelve
# por su cuenta. Meterle `current_user` acá haría que todo el módulo pida una
# cookie que el atajo no puede tener, y el síntoma sería un 401 mudo.
#
# La excepción está acotada: los tres endpoints que abre exigen el token, con el
# que no se puede hacer nada más que tocar la lista de compras, y los endpoints
# que administran los tokens sí piden `CurrentUser` en su firma —un token no
# puede emitir otro token—.
router.include_router(voz_router)

# Simulación de base de datos en memoria para inicio rápido.
#
# TODO: esto es andamiaje del template de FastAPI y no lo usa nadie —ni el
# frontend ni los tests—. Conviene borrarlo junto con `app/models/item.py`. Se
# deja detrás del login mientras tanto porque `POST /items` sin autenticar, en
# un deploy público, es una lista global que crece con cada request y nunca se
# vacía: memoria del server a cambio de nada.
db_items: List[dict] = [
    {
        "id": 1,
        "title": "Harina 0000",
        "description": "Paquete de 1kg para repostería",
        "quantity": 2,
        "price": 1200.0,
        "created_at": datetime.now(),
        "status": "pending"
    },
    {
        "id": 2,
        "title": "Leche descremada",
        "description": "Sachet de 1L",
        "quantity": 3,
        "price": 950.0,
        "created_at": datetime.now(),
        "status": "completed"
    }
]


@router.get("/health", summary="Verificación de estado de la API")
async def get_health():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Backend FastAPI Compras"
    }


@router.get(
    "/items",
    response_model=List[ItemResponse],
    summary="Obtener todos los elementos",
    dependencies=_authenticated,
)
async def get_items():
    return db_items


@router.post(
    "/items",
    response_model=ItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear nuevo elemento",
    dependencies=_authenticated,
)
async def create_item(item: ItemCreate):
    new_id = max([i["id"] for i in db_items], default=0) + 1
    new_item = {
        "id": new_id,
        "title": item.title,
        "description": item.description,
        "quantity": item.quantity,
        "price": item.price,
        "created_at": datetime.now(),
        "status": "pending"
    }
    db_items.append(new_item)
    return new_item
