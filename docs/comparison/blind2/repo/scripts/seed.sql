INSERT INTO routes (code, name, adult_fare_cents) VALUES
  ('WLG-PCN', 'Wellington - Picton', 7450),
  ('AKL-WHK', 'Auckland - Waiheke', 4200);

INSERT INTO sailings (route_code, departs_at, capacity, adult_fare_cents) VALUES
  ('WLG-PCN', '2026-12-01 08:45+13', 420, 7450),
  ('WLG-PCN', '2026-12-01 14:00+13', 420, 7450),
  ('AKL-WHK', '2026-12-01 09:00+13', 180, 4200);
