"""Configuracion del bot. Todo sale del archivo .env (nunca hardcodear credenciales)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(nombre: str, default: bool) -> bool:
    valor = os.getenv(nombre)
    if valor is None:
        return default
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes")


# Entorno: testing por ahora, despues se cambia la URL a productivo
TRAXION_URL = os.getenv("TRAXION_URL", "https://testing.telerecargas.com.ar/")
TRAXION_USER = os.getenv("TRAXION_USER", "")
TRAXION_PASS = os.getenv("TRAXION_PASS", "")

# Usuarios a buscar (estatico por ahora; despues vendra de la fuente dinamica)
USUARIO_BUSQUEDA = os.getenv("USUARIO_BUSQUEDA", "cib7kb9v3scb2sltq6yx6cf17")
# Lista separada por comas; si esta vacia se usa USUARIO_BUSQUEDA
USUARIOS = [u.strip() for u in os.getenv("USUARIOS", "").split(",") if u.strip()]
MONTO_CREDITO = os.getenv("MONTO_CREDITO", "1")

HEADLESS = _bool("HEADLESS", False)
# Si GUARDAR=false el bot llega hasta cargar el monto pero NO aprieta Guardar
GUARDAR = _bool("GUARDAR", False)
# Tope de seguridad: el bot avanza apenas ocurre el evento esperado; esto solo corta si el
# servidor no responde NUNCA (no es un tiempo de espera fijo)
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "60000"))
# Tope de la busqueda de cada usuario: si queda en "loading" mas que esto, se registra
# como TIMEOUT_BUSQUEDA y se sigue con el siguiente de la lista
TIMEOUT_BUSQUEDA_MS = int(os.getenv("TIMEOUT_BUSQUEDA_MS", "3000"))
# Al final del lote, reintentar una vez los usuarios que dieron TIMEOUT_BUSQUEDA
REINTENTAR_TIMEOUTS = _bool("REINTENTAR_TIMEOUTS", True)

# Base PostgreSQL (tabla bot.usuarios_traxion). Si DB_NAME esta vacio, el bot no usa la base
# y toma los usuarios de --usuario / --archivo / USUARIOS.
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASS = os.getenv("DB_PASS", "")
# Fallas pasajeras (ERROR / TIMEOUT_BUSQUEDA) aceptadas en corridas sucesivas antes de marcar FAILED
MAX_INTENTOS = int(os.getenv("MAX_INTENTOS", "3"))
# Sincronizacion incremental: ademas de las cuentas posteriores a la ultima importada, vuelve a mirar
# este margen hacia atras (por transacciones que confirman tarde). Las repetidas se ignoran.
SYNC_MARGEN_MIN = int(os.getenv("SYNC_MARGEN_MIN", "60"))

# Servidor Boss/Worker (servidor.py): punto de entrada HTTP para que otro sistema despierte al Worker
API_HOST = os.getenv("API_HOST", "127.0.0.1")  # 0.0.0.0 para aceptar llamadas desde otras maquinas
API_PORT = int(os.getenv("API_PORT", "8080"))
API_TOKEN = os.getenv("API_TOKEN", "")          # obligatorio: va en el header X-Token de cada llamada

DEBUG_DIR = BASE_DIR / "debug"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "resultados.db"
