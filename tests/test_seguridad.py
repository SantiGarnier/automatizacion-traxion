"""
Test de las protecciones del punto 5 de Fede, sin navegador ni base:
  - _s / _k (sesion de Traxion) nunca llegan al disco: logs, mensajes ni HTML de debug.
  - debug/: sin HTML por defecto y con borrado automatico.
  - PostgreSQL: TLS obligatorio fuera de localhost.

    python tests/test_seguridad.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import fuente_db  # noqa: E402
import traxion_bot as bot  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.LOG_DIR = tmp / "logs"
config.DEBUG_DIR = tmp / "debug"

URL = "https://testing.telerecargas.com.ar/tr4?_s=H85PVEQ04XLn7_aK76FJzxMH&_k=I3Ow3inohupHgYC5gaL_s2Ys&3"
SECRETOS = ("H85PVEQ04XLn7_aK76FJzxMH", "I3Ow3inohupHgYC5gaL_s2Ys")


def sin_secretos(texto):
    return not any(s in texto for s in SECRETOS)


# ---------------------------------------------------------------- _s / _k tapados
tapado = bot.tapar_sesion(f'<a href="{URL}">x</a> y en texto: {URL}')
assert sin_secretos(tapado), tapado
assert "_s=REDACTADO" in tapado and "_k=REDACTADO" in tapado and "&3" in tapado, tapado
assert bot.tapar_sesion("sin sesion") == "sin sesion"

bot.log(f"Timeout esperando navegacion a {URL}")
contenido_log = "".join(p.read_text(encoding="utf-8") for p in config.LOG_DIR.glob("*.log"))
assert sin_secretos(contenido_log), "el log no puede tener la sesion"
assert "_s=REDACTADO" in contenido_log


# ---------------------------------------------------------------- debug: captura si, HTML no (por defecto)
class PaginaFalsa:
    def screenshot(self, path, full_page):
        Path(path).write_bytes(b"png")

    def content(self):
        return f'<html><a href="{URL}">GESTORES</a></html>'


config.DEBUG_HTML = False
bot.guardar_debug(PaginaFalsa(), "ERROR_usuario")
assert len(list(config.DEBUG_DIR.glob("*.png"))) == 1
assert not list(config.DEBUG_DIR.glob("*.html")), "sin DEBUG_HTML no se guarda el HTML"

config.DEBUG_HTML = True
bot.guardar_debug(PaginaFalsa(), "ERROR_usuario2")
html = list(config.DEBUG_DIR.glob("*.html"))
assert len(html) == 1 and sin_secretos(html[0].read_text(encoding="utf-8")), "con DEBUG_HTML, sesion tapada"
config.DEBUG_HTML = False

# borrado automatico de lo viejo (las capturas pueden tener datos personales)
config.DEBUG_RETENCION_DIAS = 7
viejo = config.DEBUG_DIR / "20200101_000000_viejo.png"
viejo.write_bytes(b"png")
hace_10_dias = time.time() - 10 * 86400
os.utime(viejo, (hace_10_dias, hace_10_dias))
nuevos = set(config.DEBUG_DIR.iterdir()) - {viejo}
bot.limpiar_debug()
assert not viejo.exists(), "lo de mas de 7 dias se borra"
assert all(p.exists() for p in nuevos), "lo reciente se conserva"


# ---------------------------------------------------------------- PostgreSQL: TLS fuera de localhost
def params(host, sslmode="", rootcert=""):
    config.DB_HOST, config.DB_SSLMODE, config.DB_SSLROOTCERT = host, sslmode, rootcert
    return fuente_db.parametros_conexion()


assert params("localhost")["sslmode"] == "prefer", "en la maquina local no se exige TLS"
assert params("127.0.0.1", "disable")["sslmode"] == "disable", "localhost puede ir sin cifrar"
remoto = params("db-stage.abc123.us-east-1.rds.amazonaws.com", rootcert="C:/certs/global-bundle.pem")
assert remoto["sslmode"] == "verify-full", "base remota: por defecto cifra y verifica el certificado"
assert remoto["sslrootcert"] == "C:/certs/global-bundle.pem"
assert params("db.remota.com", "require")["sslmode"] == "require", "require se acepta (cifra sin verificar)"
for inseguro in ("disable", "allow", "prefer"):
    try:
        params("db.remota.com", inseguro)
        raise AssertionError(f"{inseguro} contra una base remota tendria que rechazarse")
    except fuente_db.ConexionInsegura:
        pass
try:  # nunca llega a abrir la conexion
    config.DB_HOST, config.DB_SSLMODE = "db.remota.com", "disable"
    fuente_db.BaseDatos().cerrar()
    fuente_db.BaseDatos().conexion()
    raise AssertionError("tendria que haber rechazado la conexion")
except fuente_db.ConexionInsegura as e:
    assert isinstance(e, fuente_db.psycopg.OperationalError), "la atrapan los manejos de error de base"

print("TEST SEGURIDAD OK")
