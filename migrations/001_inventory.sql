CREATE TABLE clients (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    role text NOT NULL CHECK (role IN ('reader','operator','admin')),
    secret_hash text NOT NULL UNIQUE CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz
);
CREATE TABLE products (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sku text NOT NULL UNIQUE CHECK (length(sku) BETWEEN 1 AND 80),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE warehouses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE CHECK (length(code) BETWEEN 1 AND 50),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE balances (
    warehouse_id uuid NOT NULL REFERENCES warehouses(id),
    product_id uuid NOT NULL REFERENCES products(id),
    available bigint NOT NULL DEFAULT 0 CHECK (available BETWEEN 0 AND 1000000000000),
    reserved bigint NOT NULL DEFAULT 0 CHECK (reserved BETWEEN 0 AND 1000000000000),
    PRIMARY KEY (warehouse_id, product_id)
);
CREATE TABLE receipts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    product_id uuid NOT NULL REFERENCES products(id),
    warehouse_id uuid NOT NULL REFERENCES warehouses(id),
    quantity bigint NOT NULL CHECK (quantity BETWEEN 1 AND 1000000000),
    reference text NOT NULL CHECK (length(reference) BETWEEN 1 AND 160),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE reservations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    order_ref text NOT NULL CHECK (length(order_ref) BETWEEN 1 AND 160),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','confirmed','cancelled','expired')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    closed_at timestamptz,
    CHECK (expires_at > created_at),
    CHECK ((status = 'active' AND closed_at IS NULL) OR (status <> 'active' AND closed_at IS NOT NULL))
);
CREATE TABLE reservation_items (
    reservation_id uuid NOT NULL REFERENCES reservations(id),
    product_id uuid NOT NULL REFERENCES products(id),
    warehouse_id uuid NOT NULL REFERENCES warehouses(id),
    quantity bigint NOT NULL CHECK (quantity BETWEEN 1 AND 1000000000),
    PRIMARY KEY (reservation_id, warehouse_id, product_id)
);
CREATE TABLE movements (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('receipt','reserve','confirm','cancel','expire')),
    client_id uuid NOT NULL REFERENCES clients(id),
    product_id uuid NOT NULL REFERENCES products(id),
    warehouse_id uuid NOT NULL REFERENCES warehouses(id),
    reservation_id uuid REFERENCES reservations(id),
    receipt_id uuid REFERENCES receipts(id),
    quantity bigint NOT NULL CHECK (quantity > 0),
    delta_available bigint NOT NULL,
    delta_reserved bigint NOT NULL,
    reference text NOT NULL,
    note text NOT NULL DEFAULT '' CHECK (length(note) <= 500),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
      (kind='receipt' AND delta_available=quantity AND delta_reserved=0 AND receipt_id IS NOT NULL AND reservation_id IS NULL) OR
      (kind='reserve' AND delta_available=-quantity AND delta_reserved=quantity AND reservation_id IS NOT NULL AND receipt_id IS NULL) OR
      (kind='confirm' AND delta_available=0 AND delta_reserved=-quantity AND reservation_id IS NOT NULL AND receipt_id IS NULL) OR
      (kind IN ('cancel','expire') AND delta_available=quantity AND delta_reserved=-quantity AND reservation_id IS NOT NULL AND receipt_id IS NULL)
    )
);
CREATE TABLE idempotency (
    client_id uuid NOT NULL REFERENCES clients(id),
    endpoint text NOT NULL,
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response jsonb,
    response_status integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (client_id, endpoint, key),
    CHECK ((response IS NULL AND response_status IS NULL) OR (response IS NOT NULL AND response_status BETWEEN 200 AND 499))
);
