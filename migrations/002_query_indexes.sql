CREATE INDEX reservations_expiry_idx ON reservations(expires_at, id) WHERE status='active';
CREATE INDEX reservations_client_idx ON reservations(client_id, created_at DESC, id);
CREATE INDEX movements_inventory_idx ON movements(warehouse_id, product_id, id DESC);
CREATE INDEX movements_reservation_idx ON movements(reservation_id, id);
CREATE INDEX api_keys_active_client_idx ON api_keys(client_id) WHERE revoked_at IS NULL;
