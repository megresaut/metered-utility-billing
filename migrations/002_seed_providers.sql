-- 002_seed_providers.sql — same 16 providers/capabilities as ra-avm's
-- providers table (AVMRE-specific columns stripped).

INSERT INTO providers (code, display_name, category, capabilities) VALUES
    ('aquarion',   'Aquarion',                        'water',    '{}'),
    ('cng',        'Connecticut Natural Gas',         'gas',      '{"launch": {"argv": ["--address", "{account.service_address}", "--slow-mo", "75"]}}'),
    ('eversource', 'Eversource',                      'electric', '{"launch": {"argv": ["--address", "{account.service_address}", "--slow-mo", "500"]}}'),
    ('fdwd',       'First District Water Department', 'water',    '{"launch": {"argv": ["--address", "{account.service_address}", "--slow-mo", "100"]}}'),
    ('fios',       'Verizon Fios',                    'internet', '{"launch": {"argv": ["--slow-mo", "75"]}}'),
    ('frontier',   'Frontier',                        'internet', '{}'),
    ('optimum',    'Optimum',                         'internet', '{"launch": {"argv": ["--slow-mo", "100"]}}'),
    ('rwa',        'Regional Water',                  'water',    '{"launch": {"argv": []}}'),
    ('santaguida', 'Santaguida Sanitation',           'waste',    '{}'),
    ('scg',        'Southern CT Gas',                 'gas',      '{"launch": {"argv": ["--slow-mo", "100", "--debug"]}}'),
    ('snew',       'SNEW',                            'water',    '{"launch": {"argv": ["--slow-mo", "200"]}}'),
    ('starlink',   'Starlink',                        'internet', '{}'),
    ('ttd',        'Third Taxing District',           'electric', '{"launch": {"argv": ["--slow-mo", "100"]}}'),
    ('uinet',      'United Illuminating',             'electric', '{"launch": {"argv": ["--slow-mo", "75"]}}'),
    ('winwaste',   'WinWaste',                        'waste',    '{}'),
    ('wpca',       'WPCA',                            'water',    '{"launch": {"argv": ["--slow-mo", "100"]}}')
ON CONFLICT (code) DO NOTHING;
