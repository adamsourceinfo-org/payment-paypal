-- 每一張業務表都有 caller_id。隔離是查詢層的預設，不是靠呼叫端記得帶參數。
CREATE TABLE IF NOT EXISTS api_keys (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id    text NOT NULL,
  key_hash     text NOT NULL UNIQUE,          -- sha256，不存明文
  scopes       text[] NOT NULL,
  active       boolean NOT NULL DEFAULT true,
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_hash_active ON api_keys (key_hash) WHERE active;

CREATE TABLE IF NOT EXISTS plans (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id         text NOT NULL,
  paypal_product_id text NOT NULL,
  paypal_plan_id    text NOT NULL UNIQUE,
  name              text NOT NULL,
  amount            numeric(18,4) NOT NULL,
  currency          text NOT NULL,
  interval_unit     text NOT NULL DEFAULT 'MONTH',
  interval_count    integer NOT NULL DEFAULT 1,
  status            text NOT NULL DEFAULT 'ACTIVE',
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plans_caller ON plans (caller_id, created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id       text NOT NULL,
  reference_id    text NOT NULL,               -- caller 提供的冪等鍵
  paypal_order_id text UNIQUE,
  amount          numeric(18,4) NOT NULL,
  currency        text NOT NULL,
  status          text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  captured_at     timestamptz,
  UNIQUE (caller_id, reference_id)             -- 網路重試不會變成兩筆收款
);
CREATE INDEX IF NOT EXISTS orders_caller ON orders (caller_id, created_at DESC);

CREATE TABLE IF NOT EXISTS subscriptions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id              text NOT NULL,
  plan_id                uuid NOT NULL REFERENCES plans(id),
  reference_id           text NOT NULL,
  paypal_subscription_id text UNIQUE,
  status                 text NOT NULL,
  current_period_end     timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (caller_id, reference_id)
);
CREATE INDEX IF NOT EXISTS subscriptions_caller ON subscriptions (caller_id, created_at DESC);

CREATE TABLE IF NOT EXISTS events (
  id              bigserial PRIMARY KEY,       -- 就是對外的游標
  paypal_event_id text NOT NULL UNIQUE,        -- PayPal 重送時的冪等保護
  event_type      text NOT NULL,
  -- NULL = 對應不到 caller（例如同商家帳號下其他 app 的事件）。
  -- 照樣落地保留原文，但對每個 caller 都不可見：WHERE caller_id = :me 不匹配 NULL。
  caller_id       text,
  subject_kind    text,
  subject_id      text,
  payload         jsonb NOT NULL,
  received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_caller_cursor ON events (caller_id, id);
