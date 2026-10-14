"""
Test del Boss/Worker HTTP (servidor.py) SIN navegador ni base: el Worker es simulado.
Correr con:  python tests/test_servidor.py
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

config.LOG_DIR = Path(tempfile.mkdtemp()) / "logs"
import servidor  # noqa: E402

TOKEN = "token-de-prueba-1234567890"


class WorkerFalso:
    """Simula al Worker: se queda trabajando hasta que el test lo suelta."""

    def __init__(self):
        self.llamadas = 0
        self.soltar = threading.Event()
        self.empezo = threading.Event()
        self.fallar = False

    def trabajar(self):
        self.llamadas += 1
        self.empezo.set()
        self.soltar.wait(5)
        self.soltar.clear()
        if self.fallar:
            raise RuntimeError("la base no responde")
        return {"OK": 2, "NO_ENCONTRADO": 1}


def llamar(metodo, ruta, token=TOKEN):
    req = urllib.request.Request(f"http://127.0.0.1:{puerto}{ruta}", method=metodo)
    if token is not None:
        req.add_header("X-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def esperar(condicion, segundos=5):
    fin = time.time() + segundos
    while time.time() < fin:
        if condicion():
            return
        time.sleep(0.02)
    raise AssertionError("no se cumplio a tiempo")


worker = WorkerFalso()
boss = servidor.Boss(worker)
http = servidor.ThreadingHTTPServer(("127.0.0.1", 0), servidor.crear_handler(boss, TOKEN))
puerto = http.server_address[1]
threading.Thread(target=http.serve_forever, daemon=True).start()

# Seguridad: /salud es publico; el resto pide token
assert llamar("GET", "/salud", token=None) == (200, {"ok": True})
assert llamar("GET", "/estado", token=None)[0] == 401
assert llamar("POST", "/despertar", token="otro")[0] == 401
assert worker.llamadas == 0, "con token invalido no se tiene que despertar"
assert llamar("GET", "/nada")[0] == 404

# Arranca dormido
assert llamar("GET", "/estado") == (200, {"estado": "dormido", "ultima_vuelta": None})

# Primer llamado: se despierta y responde enseguida (no espera a que termine)
assert llamar("POST", "/despertar") == (202, {"estado": "despertando"})
worker.empezo.wait(5)
assert llamar("GET", "/estado")[1]["estado"] == "despierto"

# Llamados mientras trabaja: el Boss no arranca otro Worker
assert llamar("POST", "/despertar") == (200, {"estado": "ya_despierto"})
assert llamar("POST", "/despertar") == (200, {"estado": "ya_despierto"})
assert worker.llamadas == 1

# Termina la primera vuelta -> como lo llamaron mientras tanto, da UNA vuelta mas (no dos)
worker.empezo.clear()
worker.soltar.set()
worker.empezo.wait(5)
esperar(lambda: worker.llamadas == 2)
worker.soltar.set()
esperar(lambda: llamar("GET", "/estado")[1]["estado"] == "dormido")
assert worker.llamadas == 2
ultima = llamar("GET", "/estado")[1]["ultima_vuelta"]
assert ultima["ok"] is True and ultima["resultados"] == {"OK": 2, "NO_ENCONTRADO": 1}, ultima

# Si el Worker falla, el Boss lo informa y lo manda a dormir (el Boss no se cae)
worker.fallar = True
worker.empezo.clear()
assert llamar("POST", "/despertar")[0] == 202
worker.empezo.wait(5)
worker.soltar.set()
esperar(lambda: llamar("GET", "/estado")[1]["estado"] == "dormido")
ultima = llamar("GET", "/estado")[1]["ultima_vuelta"]
assert ultima["ok"] is False and ultima["error"] == "la base no responde", ultima

# Y se puede volver a despertar despues de un error
worker.fallar = False
worker.empezo.clear()
assert llamar("POST", "/despertar")[0] == 202
worker.empezo.wait(5)
worker.soltar.set()
esperar(lambda: llamar("GET", "/estado")[1]["estado"] == "dormido")
assert llamar("GET", "/estado")[1]["ultima_vuelta"]["ok"] is True

http.shutdown()

# ---------------------------------------------------------------- vuelta inicial al arrancar
# Directo: sin ningun aviso, el Worker da una vuelta; un aviso que llega mientras tanto suma UNA vuelta mas
inicial = WorkerFalso()
boss2 = servidor.Boss(inicial)
servidor.vuelta_inicial(boss2)
inicial.empezo.wait(5)
assert boss2.despierto and inicial.llamadas == 1
assert boss2.despertar_worker() is False, "un aviso durante la vuelta inicial no arranca otro Worker"
inicial.soltar.set()
esperar(lambda: inicial.llamadas == 2)
inicial.soltar.set()
esperar(lambda: not boss2.despierto)
assert inicial.llamadas == 2


# Arranque real de main(): con DESPERTAR_AL_ARRANCAR=true da la vuelta; con false, no.
# (base y Worker falsos; serve_forever se corta enseguida como si se apretara Ctrl+C)
class BaseFalsa:
    def conexion(self):
        return None

    def cerrar(self):
        pass


def arrancar(despertar_al_arrancar):
    creados = []

    def worker_falso(guardar, headless):
        w = WorkerFalso()
        w.soltar.set()  # termina enseguida
        creados.append(w)
        return w

    def cortar(self, *a, **k):
        raise KeyboardInterrupt

    originales = (servidor.Worker, servidor.fuente_db.BaseDatos, servidor.ThreadingHTTPServer.serve_forever)
    servidor.Worker, servidor.fuente_db.BaseDatos = worker_falso, BaseFalsa
    servidor.ThreadingHTTPServer.serve_forever = cortar
    config.TRAXION_USER, config.TRAXION_PASS, config.DB_NAME = "u", "p", "prueba"
    config.API_TOKEN, config.API_HOST, config.API_PORT = TOKEN, "127.0.0.1", 0
    config.DESPERTAR_AL_ARRANCAR = despertar_al_arrancar
    try:
        assert servidor.main() == 0
    finally:
        servidor.Worker, servidor.fuente_db.BaseDatos, servidor.ThreadingHTTPServer.serve_forever = originales
    w = creados[0]
    if despertar_al_arrancar:
        assert w.empezo.wait(5), "con DESPERTAR_AL_ARRANCAR=true tiene que dar la vuelta inicial"
    else:
        time.sleep(0.3)
        assert w.llamadas == 0, "con DESPERTAR_AL_ARRANCAR=false no tiene que trabajar sin aviso"


arrancar(True)
arrancar(False)
print("TEST SERVIDOR OK")
