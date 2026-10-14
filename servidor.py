"""
Punto de entrada HTTP del bot. Sigue el flujo acordado con Fede:

    p24red  --POST /despertar-->  BOSS  --despierta-->  WORKER  --status NULL -> SUCCESS/FAILED-->  base

  - p24red: cuando se vincula un usuario nuevo, le avisa al Boss (POST /despertar).
  - Boss:   sabe si el Worker esta despierto o dormido. Si duerme, lo despierta; si ya esta
            despierto, no arranca otro: le anota que de una vuelta mas al terminar.
  - Worker: toma de bot.usuarios_traxion las cuentas con status NULL, las procesa en Traxion y
            guarda SUCCESS / FAILED. Revisa la base hasta que no quedan pendientes.
  - Cuando el Worker termina y no hay vuelta pendiente, el Boss lo manda a dormir.

Mientras duerme no consulta la base ni la plataforma: no consume nada.

    POST /despertar   -> 202 {"estado": "despertando"}       si el Worker dormia (arranca en segundo plano)
                         200 {"estado": "ya_despierto"}      si ya estaba trabajando
    GET  /estado      -> 200 {"estado": "dormido" | "despierto", "ultima_vuelta": {...}}
    GET  /salud       -> 200 {"ok": true}                    (para monitoreo; no pide token)

Todas las llamadas (menos /salud) necesitan el header  X-Token: <API_TOKEN del .env>

Uso:
    python servidor.py
    curl -X POST -H "X-Token: ..." http://localhost:8080/despertar
"""
import hmac
import json
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

import config
import fuente_db
import traxion_bot
from traxion_bot import log


class Worker:
    """
    Procesa las cuentas pendientes (status NULL) hasta vaciar la cola y guarda SUCCESS / FAILED.
    Cada cuenta se procesa como maximo una vez por vuelta: las que tienen una falla pasajera
    quedan en NULL para la proxima vez que lo despierten (asi no queda en loop si Traxion esta caido).
    """

    def __init__(self, guardar: bool, headless: bool):
        self.guardar = guardar
        self.headless = headless

    def trabajar(self) -> dict:
        """Una vuelta completa. Devuelve cuantas cuentas terminaron en cada resultado."""
        log(f"Worker: arranco (guardar={self.guardar}, headless={self.headless})")
        return dict(traxion_bot.vaciar_cola(self.guardar, self.headless))


class Boss:
    """
    Despierta al Worker y lo manda a dormir. Hay un solo Worker trabajando a la vez:
    los avisos que llegan mientras trabaja no arrancan otro, se convierten en una vuelta mas.
    El threading.Lock de aca coordina los hilos de ESTE proceso. Entre procesos distintos
    (otro servidor, un cron) coordina el candado de PostgreSQL que toma vaciar_cola().
    """

    def __init__(self, worker):
        self._worker = worker  # cualquier objeto con trabajar() -> dict
        self._lock = threading.Lock()
        self.despierto = False
        self._otra_vuelta = False
        self.ultima_vuelta: dict | None = None

    def despertar_worker(self) -> bool:
        """True si lo desperto; False si ya estaba despierto (queda anotada otra vuelta)."""
        with self._lock:
            if self.despierto:
                self._otra_vuelta = True
                return False
            self.despierto = True
            self._otra_vuelta = False
        log("Boss: despierto al Worker")
        threading.Thread(target=self._supervisar, name="worker", daemon=True).start()
        return True

    def _supervisar(self) -> None:
        """Corre el Worker las veces que haga falta y despues lo manda a dormir."""
        while True:
            vuelta = {"inicio": datetime.now().isoformat(timespec="seconds")}
            try:
                vuelta["resultados"] = self._worker.trabajar()
                vuelta["ok"] = True
            except (psycopg.Error, fuente_db.CandadoPerdido) as e:  # problema de base: alcanza con el mensaje
                log(f"Boss: el Worker fallo por la base de datos: {str(e).splitlines()[0]}")
                vuelta.update(ok=False, error=str(e).splitlines()[0][:500])
            except Exception as e:  # noqa: BLE001 - el Boss nunca se tiene que caer
                log(f"Boss: el Worker fallo: {e}\n{traceback.format_exc()}")
                vuelta.update(ok=False, error=str(e).splitlines()[0][:500])
            vuelta["fin"] = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                self.ultima_vuelta = vuelta
                if self._otra_vuelta:  # avisaron mientras trabajaba: una vuelta mas
                    self._otra_vuelta = False
                    log("Boss: llegaron avisos mientras trabajaba, el Worker da una vuelta mas")
                    continue
                self.despierto = False
                if vuelta["ok"]:
                    log("Boss: no quedan pendientes, mando a dormir al Worker")
                else:
                    log("Boss: la vuelta fallo, mando a dormir al Worker hasta el proximo aviso")
                return

    def estado(self) -> dict:
        with self._lock:
            return {"estado": "despierto" if self.despierto else "dormido", "ultima_vuelta": self.ultima_vuelta}


def crear_handler(boss: Boss, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _responder(self, codigo: int, cuerpo: dict) -> None:
            datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)

        def _autorizado(self) -> bool:
            if hmac.compare_digest(self.headers.get("X-Token", "").encode(), token.encode()):
                return True
            self._responder(401, {"error": "token invalido"})
            return False

        def do_GET(self):
            if self.path == "/salud":
                return self._responder(200, {"ok": True})
            if not self._autorizado():
                return
            if self.path == "/estado":
                return self._responder(200, boss.estado())
            self._responder(404, {"error": "no existe"})

        def do_POST(self):
            if not self._autorizado():
                return
            if self.path == "/despertar":
                if boss.despertar_worker():
                    return self._responder(202, {"estado": "despertando"})
                return self._responder(200, {"estado": "ya_despierto"})
            self._responder(404, {"error": "no existe"})

        def log_message(self, formato, *args):  # al log del bot, en vez de stderr
            log(f"HTTP {self.address_string()} {formato % args}")

    return Handler


def main() -> int:
    faltan = [n for n in ("TRAXION_USER", "TRAXION_PASS", "DB_NAME", "API_TOKEN") if not getattr(config, n)]
    if faltan:
        log(f"ERROR: faltan en el .env: {', '.join(faltan)}")
        return 1
    if len(config.API_TOKEN) < 16:
        log("ERROR: API_TOKEN es muy corto (minimo 16 caracteres)")
        return 1
    traxion_bot.avisar_config_obsoleta()
    # Probar la base al arrancar: mejor enterarse ahora que cuando lo despierten
    try:
        fuente_db.BaseDatos().conexion()
    except psycopg.Error as e:
        log(f"ERROR: no me puedo conectar a la base '{config.DB_NAME}' en {config.DB_HOST}:{config.DB_PORT} "
            f"con el usuario '{config.DB_USER}': {str(e).splitlines()[0]}")
        if not config.DB_PASS:
            log("  (DB_PASS esta vacio en el .env)")
        return 1
    finally:
        fuente_db.BaseDatos().cerrar()

    boss = Boss(Worker(guardar=config.GUARDAR, headless=config.HEADLESS))
    servidor = ThreadingHTTPServer((config.API_HOST, config.API_PORT), crear_handler(boss, config.API_TOKEN))
    log(f"Boss escuchando en http://{config.API_HOST}:{config.API_PORT} (Worker dormido)")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log("Apagando")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
