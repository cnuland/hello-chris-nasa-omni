-- Seed v0: ISS locations (SSP 50621 Rev F style codes; research/04 §B) + closed-world catalog v0 (research/04 §A).
-- Idempotent. Russian segment at module level only. no_capture zones per privacy findings (research/04).
INSERT INTO location (location_id, parent_id, kind, display_name, no_capture) VALUES
 ('USOS', NULL, 'segment', 'US Orbital Segment', false),
 ('ROS',  NULL, 'segment', 'Russian Orbital Segment', false),
 ('NOD1', 'USOS', 'module', 'Node 1 Unity', false),
 ('NOD2', 'USOS', 'module', 'Node 2 Harmony', false),
 ('NOD3', 'USOS', 'module', 'Node 3 Tranquility', false),
 ('LAB1', 'USOS', 'module', 'Destiny (US Lab)', false),
 ('COL1', 'USOS', 'module', 'Columbus', false),
 ('JPM1', 'USOS', 'module', 'Kibo JEM Pressurized Module', false),
 ('JLP1', 'USOS', 'module', 'Kibo JEM Logistics Module', false),
 ('PMM1', 'USOS', 'module', 'Leonardo PMM', false),
 ('CUP1', 'USOS', 'module', 'Cupola', false),
 ('ZVEZDA','ROS', 'module', 'Zvezda Service Module', false),
 ('ZARYA', 'ROS', 'module', 'Zarya FGB', false),
 ('NAUKA', 'ROS', 'module', 'Nauka MLM', false),
 ('NOD1D1','NOD1','zone','Node 1 deck bay 1', false),
 ('NOD1O1','NOD1','zone','Node 1 overhead bay 1', false),
 ('NOD2D4','NOD2','zone','Node 2 deck bay 4', false),
 ('NOD2O4','NOD2','zone','Node 2 overhead bay 4', false),
 ('NOD2S5','NOD2','zone','Node 2 crew quarters (stbd)', true),
 ('NOD2D5','NOD2','zone','Node 2 crew quarters (deck)', true),
 ('NOD2P5','NOD2','zone','Node 2 crew quarters (port)', true),
 ('NOD2O5','NOD2','zone','Node 2 crew quarters (ovhd)', true),
 ('NOD3F4','NOD3','zone','Node 3 WHC (toilet)', true),
 ('NOD3A3','NOD3','zone','Node 3 aft bay 3', false),
 ('LAB1S2','LAB1','zone','Lab starboard rack 2', false),
 ('LAB1P3','LAB1','zone','Lab port rack 3', false),
 ('LAB1O3','LAB1','zone','Lab overhead rack 3', false),
 ('COL1A1','COL1','zone','Columbus aft rack 1', false),
 ('JPM1F5','JPM1','zone','Kibo forward rack 5', false),
 ('PMM1D2','PMM1','zone','PMM deck 2', false)
ON CONFLICT (location_id) DO NOTHING;

INSERT INTO camera (camera_id, location_id, description) VALUES
 ('cam-lab1-fwd', 'LAB1', 'Destiny forward-looking internal camera'),
 ('cam-nod2-aft', 'NOD2', 'Node 2 aft-looking internal camera'),
 ('cam-handheld', 'USOS', 'Crew handheld camcorder (location from hints)')
ON CONFLICT (camera_id) DO NOTHING;

INSERT INTO catalog_item (catalog_id, class_name, display_name, synonyms, category, is_fungible, expected_count, home_location) VALUES
 ('ctb-half',      'cargo transfer bag', 'Cargo Transfer Bag (half)', '{CTB,"half CTB","stowage bag"}', 'container', false, NULL, 'PMM1'),
 ('ctb-single',    'cargo transfer bag', 'Cargo Transfer Bag (single)', '{CTB,"single CTB"}', 'container', false, NULL, 'PMM1'),
 ('m-bag',         'm-bag', 'M-Bag', '{"M bag","large stowage bag"}', 'container', false, NULL, 'PMM1'),
 ('ziploc',        'ziploc bag', 'Ziploc bag', '{"zip bag","plastic bag"}', 'container', true, NULL, NULL),
 ('trash-bag',     'trash bag', 'Trash bag', '{"wet trash","dry trash"}', 'container', true, NULL, NULL),
 ('laptop',        'laptop', 'SSC laptop', '{"computer","notebook","ThinkPad"}', 'object', false, 12, 'LAB1'),
 ('tablet',        'tablet', 'iPad / tablet', '{iPad,tablet}', 'object', false, 6, NULL),
 ('camera-dslr',   'camera', 'Nikon Z9 camera', '{"Nikon",DSLR,"still camera"}', 'object', false, 4, 'NOD2'),
 ('camcorder',     'camcorder', 'Camcorder', '{"video camera","HD camcorder"}', 'object', false, 2, NULL),
 ('headset',       'headset', 'Comm headset', '{headphones,"comm cap"}', 'object', false, 8, NULL),
 ('power-cable',   'cable', 'Power / data cable', '{cord,"power cord","data cable"}', 'object', true, NULL, NULL),
 ('drill-pistol',  'drill', 'Pistol-grip drill', '{"power drill",PGT,"pistol grip tool",driver}', 'object', false, 2, 'LAB1'),
 ('wrench',        'wrench', 'Wrench', '{spanner,"torque wrench"}', 'object', false, 6, NULL),
 ('scissors',      'scissors', 'Scissors', '{shears}', 'object', false, 4, NULL),
 ('duct-tape',     'duct tape', 'Duct tape roll', '{"gray tape","grey tape",tape}', 'object', false, 6, NULL),
 ('flashlight',    'flashlight', 'Flashlight', '{torch,"pen light"}', 'object', false, 6, NULL),
 ('multimeter',    'multimeter', 'Fluke multimeter', '{Fluke,"volt meter"}', 'object', false, 1, 'LAB1'),
 ('food-pack',     'food package', 'Rehydratable food pack', '{"food pouch","retort pouch","meal pack"}', 'object', true, NULL, 'NOD1'),
 ('drink-pouch',   'drink pouch', 'Beverage pouch', '{"drink bag","coffee pouch"}', 'object', true, NULL, 'NOD1'),
 ('wipes',         'wipes', 'Wet wipes', '{"cleaning wipes",towelettes}', 'object', true, NULL, NULL),
 ('towel',         'towel', 'Towel', '{cloth}', 'object', true, NULL, NULL),
 ('sample-bag',    'sample bag', 'Science sample bag', '{"specimen bag"}', 'object', true, NULL, 'COL1'),
 ('astrobee',      'astrobee robot', 'Astrobee free-flyer', '{Astrobee,"free flyer",robot}', 'object', false, 3, 'JPM1'),
 ('crew-member',   'person', 'Crew member', '{astronaut,cosmonaut,crew,person}', 'person', false, 7, NULL)
ON CONFLICT (catalog_id) DO NOTHING;
