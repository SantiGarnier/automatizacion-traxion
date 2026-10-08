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

DEBUG_DIR = BASE_DIR / "debug"
DB_PATH = BASE_DIR / "resultados.db"
