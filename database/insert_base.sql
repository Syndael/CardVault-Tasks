INSERT INTO scheduled_tasks (name, script_path, cron_expression, enabled)
VALUES
  ('Sincronizar colecciones Pokemon', 'sync_pokemon_collections.py', '0 9 * * 1', 1),
  ('Sincronizar productos Pokemon',  'sync_pokemon_products.py',   '0 15 * * *', 1),
  ('Marcar descarga forzada',        'mark_force_download.py',     NULL,         1);

INSERT INTO settings(setting_key, setting_value) VALUES ('sync.name.alter.lang.targets', 'JP,KR,CHT,CHS');
INSERT INTO settings(setting_key, setting_value) VALUES ('sync.name.alter.lang.sources', 'ES,EN');

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Sincronizar nombres alternativos', 'sync_name_alter.py', '0 16 * * *', 1);


INSERT INTO settings(setting_key, setting_value) VALUES ('export.public.images.path', '<path>');

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Exportación de imágenes para web pública', 'export_public_images.py', '0 10 * * *', 1);