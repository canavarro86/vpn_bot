-- HideWay VPN Bot — SQLite schema.
-- All write paths use transactions (see repository.py).

CREATE TABLE IF NOT EXISTS users (
    telegram_id        INTEGER PRIMARY KEY,
    username           TEXT,
    status             TEXT NOT NULL,                 -- pending_subscription | active | revoked | banned
    tier               TEXT NOT NULL DEFAULT 'free',  -- free | paid
    vpn_client_id      TEXT,                          -- UUID клиента в Xray
    access_url         TEXT,                          -- vless://... ссылка для импорта
    traffic_limit_gb   REAL NOT NULL DEFAULT 5,
    traffic_used_bytes INTEGER NOT NULL DEFAULT 0,
    paid_until         INTEGER,                       -- unix ts, NULL если free
    low_traffic_notified INTEGER NOT NULL DEFAULT 0,  -- 1 если уведомление ~100MB уже отправлено в текущем периоде
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id         INTEGER NOT NULL REFERENCES users(telegram_id),
    provider            TEXT NOT NULL,    -- stub | cryptocloud
    provider_invoice_id TEXT NOT NULL,
    amount_usd          REAL NOT NULL,
    status              TEXT NOT NULL,    -- created | pending | confirmed | failed | expired
    created_at          INTEGER NOT NULL,
    confirmed_at        INTEGER
);

CREATE TABLE IF NOT EXISTS connection_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL REFERENCES users(telegram_id),
    ip_address   TEXT,                 -- может быть NULL (см. ТЗ раздел 4)
    bytes_used   INTEGER NOT NULL,     -- агрегированный сэмпл, НЕ по доменам
    sampled_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bans (
    telegram_id  INTEGER PRIMARY KEY,
    reason       TEXT,
    banned_at    INTEGER NOT NULL,
    expires_at   INTEGER               -- NULL = permanent
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER,              -- NULL для системных событий
    action       TEXT NOT NULL,        -- client_created | client_revoked | payment_confirmed | banned | admin_grant ...
    details      TEXT,                 -- JSON-строка
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_status        ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_tier          ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_paid_until    ON users(paid_until);
CREATE INDEX IF NOT EXISTS idx_payments_tg         ON payments(telegram_id);
CREATE INDEX IF NOT EXISTS idx_conn_tg             ON connection_log(telegram_id);
CREATE INDEX IF NOT EXISTS idx_conn_sampled        ON connection_log(sampled_at);
CREATE INDEX IF NOT EXISTS idx_audit_tg            ON audit_log(telegram_id);
CREATE INDEX IF NOT EXISTS idx_audit_created       ON audit_log(created_at);
