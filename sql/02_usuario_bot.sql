-- =============================================================================
-- Usuario de base de datos propio para el bot (recomendado en stage y productivo).
-- Lo corre un administrador de la base, conectado a la base de la aplicacion,
-- DESPUES de 01_bot_usuarios_traxion.sql.
--
-- Permisos minimos:
--   - LEER public.linked_accounts y public.users (sin poder modificarlas)
--   - LEER/ESCRIBIR solo su tabla bot.usuarios_traxion
-- Cambiar la clave antes de ejecutar y guardarla solo en el .env del bot.
-- =============================================================================

CREATE ROLE traxion_bot LOGIN PASSWORD 'CAMBIAR_ESTA_CLAVE';

-- Conectarse a la base (reemplazar por el nombre real, ej. db_stage_pago24red)
-- GRANT CONNECT ON DATABASE db_stage_pago24red TO traxion_bot;

-- Tablas de la aplicacion: solo lectura
GRANT USAGE ON SCHEMA public TO traxion_bot;
GRANT SELECT ON public.linked_accounts, public.users TO traxion_bot;

-- Tabla de trabajo del bot: lectura y escritura. Vista de consulta: lectura.
GRANT USAGE ON SCHEMA bot TO traxion_bot;
GRANT SELECT, INSERT, UPDATE ON bot.usuarios_traxion TO traxion_bot;
GRANT SELECT ON bot.v_usuarios_traxion TO traxion_bot;
