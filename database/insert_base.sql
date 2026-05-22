INSERT INTO scheduled_tasks (name, script_path, cron_expression, enabled)
VALUES
  ('Sincronizar colecciones Pokemon', 'sync_pokemon_collections.py', '0 9 * * 1', 1),
  ('Sincronizar productos Pokemon',  'sync_pokemon_products.py',   '0 15 * * *', 1),
  ('Marcar descarga forzada',        'mark_force_download.py',     NULL,         1);
