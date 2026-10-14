"""
Conexion del bot con PostgreSQL (tabla bot.usuarios_traxion, ver sql/01_bot_usuarios_traxion.sql).

- De public.linked_accounts el bot solo LEE 5 columnas (id, provider, status, external_user_id,
  created_at). No lee public.users. Todo lo que escribe va al esquema "bot".
- Cada fila se identifica por linked_account_id (la clave). usuario_traxion NO es unico: dos vinculos
  de Pago24 pueden apuntar al mismo usuario de Traxion, y cada uno tiene su propio resultado.

Contrato de la tabla (acordado con Fede):
    status NULL     -> pendiente, se procesa en la proxima corrida
    status SUCCESS  -> listo
    status FAILED   -> no se pudo; motivo en status_message

Concurrencia (dos procesos a la vez: servidor + cron, o dos servidores):
    1. Candado por corrida: pg_try_advisory_lock. Solo una corrida trabaja; la otra sale enseguida.
       Esta atado a la conexion: si el proceso muere, PostgreSQL lo libera solo.
    2. Reserva por fila: antes de procesar una cuenta se marca reclamado_en / reclamado_por con un
       UPDATE atomico. Si otro proceso la reservo, se saltea. La reserva se libera al guardar el
       resultado y vence sola (RECLAMO_VENCE_MIN) si el proceso murio con la cuenta reservada.
"""
import os
import socket
import uuid
from typing import NamedTuple

import psycopg

import config

# Como se traduce el resultado del bot a status:
EXITO = {"OK", "SIN_CAMBIOS"}                      # -> SUCCESS
FALLA_DEFINITIVA = {"NO_ENCONTRADO", "VARIAS_CUENTAS", "NO_COINCIDE"}  # -> FAILED (reintentar no cambia nada)
FALLA_PASAJERA = {"ERROR", "TIMEOUT_BUSQUEDA"}     # -> sigue NULL y se reintenta; a los MAX_INTENTOS -> FAILED
# El resto (PRUEBA: no se guardo por GUARDAR=false; NO_PROCESADO: el bot no llego a procesarla)
# deja status NULL sin sumar intentos: no es culpa de la cuenta.

# Clave del candado de PostgreSQL que coordina las corridas (cualquier entero fijo; todos los procesos
# del bot usan el mismo). Se ve en pg_locks como locktype = 'advisory', objid = 900001.
CANDADO_ID = 900001

# Identifica a este proceso en reclamado_por (maquina:pid:corrida)
PROCESO = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Cuenta(NamedTuple):
    """Un trabajo para el bot. linked_account_id es None en las pruebas manuales (--usuario/--archivo)."""
    linked_account_id: str | None
    usuario: str


class CandadoPerdido(Exception):
    """La conexion se cayo y, al reconectar, otra corrida ya tenia el candado: hay que cortar la vuelta."""


class ConexionInsegura(psycopg.OperationalError):
    """
    La base esta en otro host y la configuracion permitiria conectarse sin cifrar: no se conecta.
    Hereda de psycopg.OperationalError para que todos los manejos de "error de base" la atrapen.
    """


_HOSTS_LOCALES = {"", "localhost", "127.0.0.1", "::1"}
_MODOS_SIN_GARANTIA = {"disable", "allow", "prefer"}  # pueden terminar conectando en claro


def parametros_conexion() -> dict:
    """
    Arma los parametros de psycopg.connect desde el .env, con TLS obligatorio fuera de localhost.
    Por canal en claro viajarian DB_PASS y los ids de cuenta.
    """
    local = config.DB_HOST.strip().lower() in _HOSTS_LOCALES
    sslmode = config.DB_SSLMODE or ("prefer" if local else "verify-full")
    if not local and sslmode in _MODOS_SIN_GARANTIA:
        raise ConexionInsegura(f"DB_SSLMODE={sslmode} no esta permitido para la base remota {config.DB_HOST}: "
                               "usar verify-full (recomendado) o require")
    parametros = {
        "host": config.DB_HOST,
        "port": config.DB_PORT,
        "dbname": config.DB_NAME,
        "user": config.DB_USER,
        "password": config.DB_PASS,
        "sslmode": sslmode,
        "connect_timeout": 10,
        "application_name": "traxion_bot",
        "autocommit": True,  # cada actualizacion queda guardada al instante, aunque el bot se corte despues
    }
    if config.DB_SSLROOTCERT:
        parametros["sslrootcert"] = config.DB_SSLROOTCERT
    return parametros


# Misma sentencia que sql/01_bot_usuarios_traxion.sql. INCREMENTAL: solo trae las cuentas
# creadas desde la ultima importada (con un margen por transacciones que confirman tarde) y
# no reescribe las que ya estan. Usa el indice de linked_accounts por provider.
SQL_SINCRONIZAR = """
INSERT INTO bot.usuarios_traxion (linked_account_id, usuario_traxion, origen_creado_en)
SELECT la.id, la.external_user_id, la.created_at
FROM public.linked_accounts la
WHERE la.provider = 'traxion'
  AND la.external_user_id IS NOT NULL
  AND la.created_at >= COALESCE((SELECT MAX(origen_creado_en) FROM bot.usuarios_traxion), '-infinity')
                       - make_interval(mins => %(margen_min)s)
ON CONFLICT (linked_account_id) DO NOTHING
"""

# Cola de trabajo: status NULL (indice parcial idx_usuarios_traxion_pendientes), sin las que tiene
# reservadas otra corrida. El estado del vinculo se lee EN VIVO de linked_accounts por la FK.
SQL_PENDIENTES = """
SELECT b.linked_account_id, b.usuario_traxion
FROM bot.usuarios_traxion b
JOIN public.linked_accounts la ON la.id = b.linked_account_id
WHERE b.status IS NULL
  AND la.status = 'active'
  AND (b.reclamado_en IS NULL OR b.reclamado_en < now() - make_interval(mins => %(vence_min)s))
ORDER BY b.origen_creado_en, b.linked_account_id
LIMIT %(limite)s
"""

# Reserva atomica: si dos procesos la piden a la vez, PostgreSQL bloquea la fila y el segundo
# re-evalua el WHERE con la reserva del primero, asi que solo uno recibe la fila en el RETURNING.
# El mismo proceso puede volver a reservarla (reintento dentro del mismo lote).
SQL_RECLAMAR = """
UPDATE bot.usuarios_traxion
SET reclamado_en = now(), reclamado_por = %(proceso)s
WHERE linked_account_id = %(id)s
  AND status IS NULL
  AND (reclamado_en IS NULL
       OR reclamado_por = %(proceso)s
       OR reclamado_en < now() - make_interval(mins => %(vence_min)s))
RETURNING linked_account_id
"""

# Guarda el resultado POR linked_account_id y libera la reserva. No pisa una fila que tiene
# reservada OTRO proceso (por ejemplo, si la reserva de este vencio y otra corrida la tomo).
# En el SET, "intentos" del lado derecho es el valor ANTERIOR de la fila.
SQL_ACTUALIZAR = """
UPDATE bot.usuarios_traxion SET
    status = CASE
        WHEN %(exito)s THEN 'SUCCESS'
        WHEN %(falla_definitiva)s THEN 'FAILED'
        WHEN %(falla_pasajera)s AND intentos + 1 >= %(max_intentos)s THEN 'FAILED'
        ELSE NULL
    END,
    status_message = CASE
        WHEN %(exito)s THEN NULL
        WHEN %(falla_pasajera)s AND intentos + 1 >= %(max_intentos)s
            THEN %(mensaje)s || ' (' || (intentos + 1) || ' intentos)'
        ELSE %(mensaje)s
    END,
    resultado      = %(resultado)s,
    intentos       = intentos + CASE WHEN %(falla_pasajera)s THEN 1 ELSE 0 END,
    cuenta_numero  = COALESCE(%(cuenta_numero)s, cuenta_numero),
    monto_anterior = COALESCE(%(monto_anterior)s, monto_anterior),
    procesado_en   = now(),
    reclamado_en   = NULL,
    reclamado_por  = NULL
WHERE linked_account_id = %(id)s
  AND (reclamado_por IS NULL OR reclamado_por = %(proceso)s)
"""


class BaseDatos:
    """
    Unica conexion a PostgreSQL de todo el bot (singleton). Los datos salen del .env
    (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS) via config.

        db = BaseDatos()          # siempre devuelve la misma instancia
        db.ejecutar(sql, params)  # todas las consultas pasan por aca, siempre con parametros

    Se conecta recien la primera vez que se usa y se reconecta sola si la conexion se cayo.
    Si tenia el candado de la corrida, al reconectar lo vuelve a pedir; si otra corrida lo tomo
    mientras tanto, lanza CandadoPerdido (seguir sin el candado no seria seguro).
    """

    _instancia: "BaseDatos | None" = None

    def __new__(cls) -> "BaseDatos":
        if cls._instancia is None:
            cls._instancia = super().__new__(cls)
            cls._instancia._conn = None
            cls._instancia._candado = False
        return cls._instancia

    def conexion(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed or self._conn.broken:
            self._conn = psycopg.connect(**parametros_conexion())
            if self._candado and not self._pedir_candado():
                self._candado = False
                raise CandadoPerdido("Se cayo la conexion a la base y, al reconectar, otra corrida "
                                     "ya tenia el candado: se corta esta vuelta")
        return self._conn

    def _pedir_candado(self) -> bool:
        return self._conn.execute("SELECT pg_try_advisory_lock(%s)", (CANDADO_ID,)).fetchone()[0]

    def tomar_candado(self) -> bool:
        """True si esta corrida queda como la unica trabajando; False si otra ya tiene el candado."""
        if not self._candado:
            self.conexion()
            self._candado = self._pedir_candado()
        return self._candado

    def soltar_candado(self) -> None:
        if self._candado and self._conn is not None and not self._conn.closed and not self._conn.broken:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (CANDADO_ID,))
        self._candado = False

    def ejecutar(self, sql: str, params: dict | None = None) -> psycopg.Cursor:
        """Ejecuta una consulta parametrizada (nunca armar SQL concatenando valores)."""
        return self.conexion().execute(sql, params)

    def cerrar(self) -> None:
        """Cierra la conexion (y con ella se libera el candado, si lo tenia)."""
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None
        self._candado = False


def sincronizar() -> int:
    """Importa las cuentas nuevas de linked_accounts. Devuelve cuantas agrego."""
    return BaseDatos().ejecutar(SQL_SINCRONIZAR, {"margen_min": config.SYNC_MARGEN_MIN}).rowcount


def pendientes(limite: int | None = None) -> list[Cuenta]:
    filas = BaseDatos().ejecutar(SQL_PENDIENTES, {"limite": limite, "vence_min": config.RECLAMO_VENCE_MIN})
    return [Cuenta(*fila) for fila in filas.fetchall()]


def reclamar(cuenta: Cuenta) -> bool:
    """Reserva la cuenta para este proceso. False si la tiene otro proceso o ya no esta pendiente."""
    return BaseDatos().ejecutar(SQL_RECLAMAR, {
        "id": cuenta.linked_account_id, "proceso": PROCESO, "vence_min": config.RECLAMO_VENCE_MIN,
    }).fetchone() is not None


def actualizar(cuenta: Cuenta, resultado: str, detalle: str,
               cuenta_numero: str | None, monto_anterior: str | None) -> bool:
    """
    Guarda el resultado de una cuenta (por linked_account_id), traducido a status SUCCESS / FAILED / NULL,
    y libera su reserva. False si no se escribio porque la fila la tiene reservada otro proceso.
    """
    mensaje = f"{resultado}: {detalle}" if detalle else resultado
    return BaseDatos().ejecutar(SQL_ACTUALIZAR, {
        "id": cuenta.linked_account_id,
        "proceso": PROCESO,
        "resultado": resultado,
        "mensaje": mensaje,
        "exito": resultado in EXITO,
        "falla_definitiva": resultado in FALLA_DEFINITIVA,
        "falla_pasajera": resultado in FALLA_PASAJERA,
        "max_intentos": config.MAX_INTENTOS,
        "cuenta_numero": cuenta_numero,
        "monto_anterior": monto_anterior,
    }).rowcount > 0
