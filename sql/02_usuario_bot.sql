-- =============================================================================
-- Usuario de base de datos propio para el bot (recomendado en stage y productivo).
-- Lo corre un administrador de la base, conectado a la base de la aplicacion,
-- DESPUES de 01_bot_usuarios_traxion.sql.
--
-- Permisos minimos (principio de menor privilegio):
--   - public.linked_accounts: SOLO LECTURA y SOLO de las 5 columnas que el bot usa.
--     Cualquier otra columna (metadata_json, last_error, ...) la base la rechaza.
--   - public.users: NINGUN acceso (el bot no necesita emails, CUIT ni datos personales).
--   - bot.usuarios_traxion: lectura y escritura (su tabla de trabajo).
--   - bot.v_usuarios_traxion: NINGUN acceso. Es para consulta humana y lee public.users;
--     como una vista corre con los permisos de su duenio, darsela al bot le abriria una via a users.
-- Cambiar la clave antes de ejecutar y guardarla solo en el .env del bot.
-- =============================================================================

CREATE ROLE traxion_bot LOGIN PASSWORD 'CAMBIAR_ESTA_CLAVE';

-- Conectarse a la base (reemplazar por el nombre real, ej. db_stage_pago24red)
-- GRANT CONNECT ON DATABASE db_stage_pago24red TO traxion_bot;

-- Tabla de la aplicacion: solo lectura de las columnas que usa el bot
GRANT USAGE ON SCHEMA public TO traxion_bot;
GRANT SELECT (id, provider, status, external_user_id, created_at) ON public.linked_accounts TO traxion_bot;

-- Tabla de trabajo del bot: lectura y escritura
GRANT USAGE ON SCHEMA bot TO traxion_bot;
GRANT SELECT, INSERT, UPDATE ON bot.usuarios_traxion TO traxion_bot;
