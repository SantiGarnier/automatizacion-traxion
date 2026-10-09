-- =============================================================================
-- Tabla de trabajo del bot Traxion (esquema propio "bot", separado de "public")
--
-- Las tablas de la aplicacion (public.linked_accounts, public.users) solo se LEEN;
-- el bot nunca las modifica. Todo lo que el bot escribe va al esquema "bot".
--
-- Ejecutar completo en DBeaver con Alt+X (Ejecutar script). Se puede correr mas
-- de una vez sin romper nada.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS bot;

-- Un registro por cuenta de Pago24 Red vinculada a Traxion.
--   status NULL      -> pendiente: el bot la procesa en la proxima corrida
--   status SUCCESS   -> Monto credito quedo en 1 (o ya lo tenia)
--   status FAILED    -> no se pudo; el motivo esta en status_message
CREATE TABLE IF NOT EXISTS bot.usuarios_traxion (
    linked_account_id text PRIMARY KEY
        REFERENCES public.linked_accounts (id) ON DELETE CASCADE,  -- si la app borra el vinculo, se borra aca
    usuario_traxion   text        NOT NULL,                  -- linked_accounts.external_user_id: lo que se busca en Traxion
    origen_creado_en  timestamptz NOT NULL,                  -- linked_accounts.created_at: marca de agua de la sincronizacion

    -- Manejado por el bot
    status            text CHECK (status IN ('SUCCESS', 'FAILED')),  -- NULL = pendiente
    status_message    text,                                  -- motivo del FAILED (o del ultimo error si se va a reintentar)
    resultado         text,                                  -- detalle: OK, SIN_CAMBIOS, NO_ENCONTRADO, VARIAS_CUENTAS, ERROR, TIMEOUT_BUSQUEDA, PRUEBA
    intentos          integer     NOT NULL DEFAULT 0,        -- errores pasajeros acumulados (a los MAX_INTENTOS pasa a FAILED)
    cuenta_numero     text,                                  -- numero de cuenta encontrado en Traxion
    monto_anterior    text,                                  -- "Monto credito" antes de que el bot lo cambie
    procesado_en      timestamptz,                           -- ultima vez que el bot lo proceso

    importado_en      timestamptz NOT NULL DEFAULT now()
);

-- Cola de trabajo: indice PARCIAL, solo con las cuentas pendientes (status NULL).
-- Las ya procesadas (SUCCESS / FAILED) no ocupan lugar en el indice.
CREATE INDEX IF NOT EXISTS idx_usuarios_traxion_pendientes
    ON bot.usuarios_traxion (origen_creado_en, linked_account_id)
    WHERE status IS NULL;

-- Marca de agua (MAX) y actualizacion del resultado por usuario
CREATE INDEX IF NOT EXISTS idx_usuarios_traxion_origen  ON bot.usuarios_traxion (origen_creado_en);
CREATE INDEX IF NOT EXISTS idx_usuarios_traxion_usuario ON bot.usuarios_traxion (usuario_traxion);

-- Vista para consultar a simple vista: datos del bot + vinculo + comercio (join por la FK)
CREATE OR REPLACE VIEW bot.v_usuarios_traxion AS
SELECT b.usuario_traxion,
       u.commerce_name AS comercio,
       la.status       AS estado_vinculo,
       b.status,
       b.status_message,
       b.resultado,
       b.intentos,
       b.cuenta_numero,
       b.monto_anterior,
       b.procesado_en,
       b.linked_account_id,
       la.user_id
FROM bot.usuarios_traxion b
JOIN public.linked_accounts la ON la.id = b.linked_account_id
LEFT JOIN public.users u      ON u.id  = la.user_id;

-- -----------------------------------------------------------------------------
-- Sincronizacion INCREMENTAL: solo trae las cuentas creadas desde la ultima importada
-- (menos 1 hora de margen, por transacciones que confirman tarde). No reescribe las
-- que ya estan. La primera vez (tabla vacia) trae todas. Entran con status NULL.
-- El bot corre esta misma sentencia al arrancar cada lote (ver fuente_db.py).
-- -----------------------------------------------------------------------------
INSERT INTO bot.usuarios_traxion (linked_account_id, usuario_traxion, origen_creado_en)
SELECT la.id, la.external_user_id, la.created_at
FROM public.linked_accounts la
WHERE la.provider = 'traxion'
  AND la.external_user_id IS NOT NULL
  AND la.created_at >= COALESCE((SELECT MAX(origen_creado_en) FROM bot.usuarios_traxion), '-infinity')
                       - interval '1 hour'
ON CONFLICT (linked_account_id) DO NOTHING;

-- Verificacion
SELECT estado_vinculo, COALESCE(status, 'NULL (pendiente)') AS status, COUNT(*) AS cantidad
FROM bot.v_usuarios_traxion
GROUP BY 1, 2
ORDER BY 1, 2;
