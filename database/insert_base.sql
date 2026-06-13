INSERT INTO scheduled_tasks (name, script_path, cron_expression, enabled)
VALUES
  ('Sincronizar colecciones Pokemon', 'sync_pokemon_collections.py', '0 9 * * 1', 1),
  ('Sincronizar productos Pokemon',  'sync_pokemon_products.py',   '0 15 * * *', 1),
  ('Marcar descarga forzada',        'mark_force_download.py',     NULL,         1);

INSERT INTO scheduled_tasks (name, script_path, cron_expression, enabled)
VALUES
  ('Sincronizar colecciones Magic',    'sync_magic_collections.py',    '15 9 * * 1', 1),
  ('Sincronizar colecciones Digimon',  'sync_digimon_collections.py',  '30 9 * * 1', 1),
  ('Sincronizar colecciones Yu-Gi-Oh', 'sync_yugioh_collections.py',   '45 9 * * 1', 1),
  ('Sincronizar productos Magic',      'sync_magic_products.py',       '15 15 * * *', 1),
  ('Sincronizar productos Digimon',    'sync_digimon_products.py',     '30 15 * * *', 1),
  ('Sincronizar productos Yu-Gi-Oh',   'sync_yugioh_products.py',      '45 15 * * *', 1),
  ('Sincronizar colecciones One Piece', 'sync_one_piece_collections.py', '0 10 * * 1', 1),
  ('Sincronizar productos One Piece',  'sync_one_piece_products.py',   '0 16 * * *', 1);

INSERT INTO settings(setting_key, setting_value) VALUES ('sync.name.alter.lang.targets', 'JP,KR,CHT,CHS');
INSERT INTO settings(setting_key, setting_value) VALUES ('sync.name.alter.lang.sources', 'ES,EN');

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Sincronizar nombres alternativos', 'sync_name_alter.py', '0 16 * * *', 1);

INSERT INTO settings(setting_key, setting_value) VALUES ('export.public.images.path', '<path>');

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Exportación de imágenes para web pública', 'export_public_images.py', '0 10 * * *', 1);

INSERT INTO settings (setting_key, setting_value) VALUES
('sync.magic.collections.api.base', 'https://api.scryfall.com'),
('sync.magic.collections.card.type', 'MTG'),
('sync.magic.collections.migration.languages', 'en-EN;es-ES;'),
('sync.magic.products.api.base', 'https://api.scryfall.com'),
('sync.magic.products.card.type', 'MTG'),
('sync.magic.products.migration.languages', 'en;es;'),
('sync.magic.products.img.path', './../.files/products_images'),
('sync.magic.products.img.path.pattern', '{card_type}/{is_manual}/{collection_code}'),
('sync.digimon.collections.api.base', 'https://www.digimoncard.io/api-public'),
('sync.digimon.collections.card.type', 'DIG'),
('sync.digimon.collections.migration.languages', 'en-EN;'),
('sync.digimon.products.api.base', 'https://www.digimoncard.io/api-public'),
('sync.digimon.products.card.type', 'DIG'),
('sync.digimon.products.migration.languages', 'en;'),
('sync.digimon.products.img.path', './../.files/products_images'),
('sync.digimon.products.img.path.pattern', '{card_type}/{is_manual}/{collection_code}'),
('tasks.log.path', './../.files/task_logs'),
('sync.yugioh.collections.api.base', 'https://db.ygoprodeck.com/api/v7'),
('sync.yugioh.collections.card.type', 'YUG'),
('sync.yugioh.collections.migration.languages', 'en-EN;es-ES;'),
('sync.yugioh.products.api.base', 'https://db.ygoprodeck.com/api/v7'),
('sync.yugioh.products.card.type', 'YUG'),
('sync.yugioh.products.migration.languages', 'en;es;ja;'),
('sync.yugioh.products.img.path', './../.files/products_images'),
('sync.yugioh.products.img.path.pattern', '{card_type}/{is_manual}/{collection_code}'),
('sync.one-piece.collections.api.base', 'https://optcgapi.com'),
('sync.one-piece.collections.card.type', 'OP'),
('sync.one-piece.collections.migration.languages', 'en-EN;'),
('sync.one-piece.products.api.base', 'https://optcgapi.com'),
('sync.one-piece.products.card.type', 'OP'),
('sync.one-piece.products.migration.languages', 'en;es;'),
('sync.one-piece.products.img.path', './../.files/products_images'),
('sync.one-piece.products.img.path.pattern', '{card_type}/{is_manual}/{collection_code}');

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Buscar productos Digimon (alt arts)', 'find_digimon_products.py', '0 12 * * 1', 1);

INSERT INTO settings(setting_key, setting_value)
VALUES ('sync.digimon.products.filter.collections', '');

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Scrapeo PokeCollector (cartas JP)', 'sync_pokemon_pokecollector.py', '0 8 1,15 * *', 1);

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('CardMarket checker de precios', 'cardmarket_checker.py', '30 8 1,15 * *', 1);

INSERT INTO scheduled_tasks(name, script_path, cron_expression, enabled)
VALUES ('Limpiador de imágenes', 'clean_orphan_product_images.py', '0 6 1,15 * *', 1);