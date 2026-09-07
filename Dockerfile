# Imagen única: el SPA compilado y la API en el mismo server.
#
# Un solo servicio y no dos por una razón concreta: así la puerta de acceso
# (`ACCESS_KEY`) cubre las páginas, la API y `/docs` con una sola
# implementación, y la cookie de sesión puede quedarse en `SameSite=lax` —que es
# lo que bloquea CSRF— en lugar de necesitar `none` por tener el frontend en otro
# dominio.

# --- 1. build del frontend ---------------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /app/frontend

# El manifiesto va antes que el código para que `npm ci` quede cacheado: cambiar
# un componente no debería reinstalar node_modules.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# --- 2. runtime --------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Mismo criterio de cacheo que arriba.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

# El `dist/` del build anterior. `STATIC_DIR` apunta acá en fly.toml.
COPY --from=frontend /app/frontend/dist ./static

# La base vive en un volumen montado en /data, no en la imagen: si viviera acá,
# cada deploy reemplazaría el filesystem y se llevaría puestas las cuentas, las
# listas y el histórico de precios.
RUN mkdir -p /data

# No corre como root: si alguna vez se escapa algo por la app, que no sea con
# todos los permisos de la máquina.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app /data
USER app

EXPOSE 8000

# Un solo worker a propósito. Dos motivos, y los dos importan acá:
#   * SQLite en un volumen no admite varios procesos escribiendo.
#   * El rate limiter de login y el espaciado de requests a los supermercados
#     viven en memoria del proceso: con N workers, cada uno cuenta por su lado y
#     el techo real se multiplica por N. Es justo lo que no queremos.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
