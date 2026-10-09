"""
Conexion del bot con PostgreSQL (tabla bot.usuarios_traxion, ver sql/01_bot_usuarios_traxion.sql).

- Las tablas de la aplicacion (public.linked_accounts, public.users) solo se LEEN.
- Todo lo que escribe el bot va al esquema "bot".

Contrato de la tabla (acordado con Fede):
    status NULL     -> pendiente, se procesa en la proxima corrida
    status SUCCESS  -> listo
    status FAILED   -> no se pudo; motivo en status_message
"""
import psycopg

import config

# Como se traduce el resultado del bot a status:
EXITO = {"OK", "SIN_CAMBIOS"}                      # -> SUCCESS
FALLA_DEFINITIVA = {"NO_ENCONTRADO", "VARIAS_CUENTAS"}  # -> FAILED (reintentar no cambia nada)
FALLA_PASAJERA = {"ERROR", "TIMEOUT_BUSQUEDA"}     # -> sigue NULL y se reintenta; a los MAX_INTENTOS -> FAILED
# El resto (PRUEBA: no se guardo por GUARDAR=false; NO_PROCESADO: el bot no llego a procesarla)
# deja status NULL sin sumar intentos: no es culpa de la cuenta.

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

# Cola de trabajo: status NULL (indice parcial idx_usuarios_traxion_pendientes).
# El estado del vinculo se lee EN VIVO de linked_accounts por la FK (busqueda por clave primaria).
SQL_PENDIENTES = """
SELECT b.usuario_traxion
FROM bot.usuarios_traxion b
JOIN public.linked_accounts la ON la.id = b.linked_account_id
WHERE b.status IS NULL
  AND la.status = 'active'
ORDER BY b.origen_creado_en, b.linked_account_id
LIMIT %(limite)s
"""

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
    procesado_en   = now()
WHERE usuario_traxion = %(usuario)s
"""


class BaseDatos:
    """
    Unica conexion a PostgreSQL de todo el bot (singleton). Los datos salen del .env
    (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS) via config.

        db = BaseDatos()          # siempre devuelve la misma instancia
        db.ejecutar(sql, params)  # todas las consultas pasan por aca, siempre con parametros

    Se conecta recien la primera vez que se usa y se reconecta sola si la conexion se cayo.
    """

    _instancia: "BaseDatos | None" = None

    def __new__(cls) -> "BaseDatos":
        if cls._instancia is None:
            cls._instancia = super().__new__(cls)
            cls._instancia._conn = None
        return cls._instancia

    def conexion(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed or self._conn.broken:
            self._conn = psycopg.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                dbname=config.DB_NAME,
                user=config.DB_USER,
                password=config.DB_PASS,
                connect_timeout=10,
                application_name="traxion_bot",
                autocommit=True,  # cada actualizacion queda guardada al instante, aunque el bot se corte despues
            )
        return self._conn

    def ejecutar(self, sql: str, params: dict | None = None) -> psycopg.Cursor:
        """Ejecuta una consulta parametrizada (nunca armar SQL concatenando valores)."""
        return self.conexion().execute(sql, params)

    def cerrar(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


def sincronizar() -> int:
    """Importa las cuentas nuevas de linked_accounts. Devuelve cuantas agrego."""
    return BaseDatos().ejecutar(SQL_SINCRONIZAR, {"margen_min": config.SYNC_MARGEN_MIN}).rowcount


def pendientes(limite: int | None = None) -> list[str]:
    return [fila[0] for fila in BaseDatos().ejecutar(SQL_PENDIENTES, {"limite": limite}).fetchall()]


def actualizar(usuario: str, resultado: str, detalle: str,
               cuenta_numero: str | None, monto_anterior: str | None) -> None:
    """Guarda el resultado del bot para un usuario, traducido a status SUCCESS / FAILED / NULL."""
    mensaje = f"{resultado}: {detalle}" if detalle else resultado
    BaseDatos().ejecutar(SQL_ACTUALIZAR, {
        "usuario": usuario,
        "resultado": resultado,
        "mensaje": mensaje,
        "exito": resultado in EXITO,
        "falla_definitiva": resultado in FALLA_DEFINITIVA,
        "falla_pasajera": resultado in FALLA_PASAJERA,
        "max_intentos": config.MAX_INTENTOS,
        "cuenta_numero": cuenta_numero,
        "monto_anterior": monto_anterior,
    })
