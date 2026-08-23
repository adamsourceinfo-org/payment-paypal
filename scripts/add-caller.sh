#!/usr/bin/env bash
# 新增一個 caller 的 API key。
#
# 刻意沒有 admin API、也沒有 bootstrap admin key —— 一把能製造其他 key 的
# 萬能鑰匙，它的長期風險大於它省下的麻煩。代價是這個動作要人跑，
# 所以把它寫成腳本，讓「手動」不等於「痛苦」。
#
# 用法：
#   ./scripts/add-caller.sh dev my-service "orders:read,orders:write" "備註"
set -euo pipefail

ENVIRONMENT="${1:?用法: add-caller.sh <dev|prod> <caller_id> <scopes> [note]}"
CALLER_ID="${2:?缺少 caller_id}"
SCOPES="${3:?缺少 scopes（逗號分隔）}"
NOTE="${4:-}"

PROJECT="adamsourceinfo-${ENVIRONMENT}"
INSTANCE="payment-paypal-pg"
DB="payment_paypal"
PORT=5433

if [[ -z "${PGPASSWORD:-}" ]]; then
  cat >&2 <<'HELP'
請先設定 PGPASSWORD —— 內建 postgres 帳號的密碼。

人連 DB 用內建的 postgres 帳號；run-runtime 那個 IAM 使用者是給服務用的。
密碼沒有存在任何地方（刻意的），忘記就直接重設一個：

  PROJECT=adamsourceinfo-dev
  export PGPASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  gcloud sql users set-password postgres --instance=payment-paypal-pg     --project="$PROJECT" --password="$PGPASSWORD" --quiet

注意：資料表是 app 的 run_migrations() 以 run-runtime 身分建的，postgres 預設
沒有權限。第一次要先讓 postgres 成為該角色的成員（只需做一次）：

  GRANT "run-runtime@adamsourceinfo-dev.iam" TO postgres;
HELP
  exit 1
fi

# 產生 key 並算 hash。DB 裡只存 hash，明文只在這裡出現一次。
KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
KEY_HASH="$(printf '%s' "$KEY" | shasum -a 256 | cut -d' ' -f1)"

# scopes 轉成 Postgres array literal
SCOPES_SQL="$(python3 -c "
import sys
items = [s.strip() for s in sys.argv[1].split(',') if s.strip()]
print('ARRAY[' + ','.join(\"'\" + i.replace(\"'\", \"''\") + \"'\" for i in items) + ']::text[]')
" "$SCOPES")"

cloud-sql-proxy "${PROJECT}:asia-east1:${INSTANCE}" --port "$PORT" \
  --token "$(gcloud auth print-access-token)" >/tmp/add-caller-proxy.log 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 1; done

psql -h 127.0.0.1 -p "$PORT" -U postgres -d "$DB" -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO api_keys (caller_id, key_hash, scopes, note)
VALUES ('${CALLER_ID}', '${KEY_HASH}', ${SCOPES_SQL}, $(
  if [[ -n "$NOTE" ]]; then echo "'${NOTE}'"; else echo "NULL"; fi));
SQL

cat <<MSG

  caller : ${CALLER_ID}
  環境   : ${ENVIRONMENT}
  scopes : ${SCOPES}

  API key（只會顯示這一次，DB 裡只有 sha256）：

    ${KEY}

MSG
