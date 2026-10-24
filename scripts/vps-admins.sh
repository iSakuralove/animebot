#!/usr/bin/env bash
# 在本机（Windows Git Bash / Linux / macOS）远程管理 VPS 上 .env 的管理员名单。
#
# 用法:
#   ANIMEBOT_VPS=root@<vps> scripts/vps-admins.sh list
#   ANIMEBOT_VPS=root@<vps> scripts/vps-admins.sh add 123456
#   ANIMEBOT_VPS=root@<vps> scripts/vps-admins.sh del 123456
#
# - VPS 地址: 环境变量 ANIMEBOT_VPS（必填）。本机没配过的，见 CLAUDE.local.md。
# - 部署目录: 环境变量 ANIMEBOT_VPS_DIR，默认 /root/animebot。
# - add / del 改完 .env 会重启 animebot 生效；list 只读。
# - del 拒绝删到最后一个 —— 全删了没人能进后台，只能上 VPS 手修 .env。
set -euo pipefail

HOST="${ANIMEBOT_VPS:?先设 ANIMEBOT_VPS=root@<vps>（地址见 CLAUDE.local.md）}"
DIR="${ANIMEBOT_VPS_DIR:-/root/animebot}"
ACTION="${1:-}"
ID="${2:-}"

case "$ACTION" in
  list) ;;
  add|del)
    [[ "$ID" =~ ^[0-9]+$ ]] || { echo "用法: $0 list | add <user_id> | del <user_id>" >&2; exit 2; }
    ;;
  *) echo "用法: $0 list | add <user_id> | del <user_id>" >&2; exit 2 ;;
esac

# ssh 把多个参数拼成一个字符串、远端再按空白分词 —— 空参数会被吞掉，
# 所以 id 永远传占位符，远端只在 add/del 时才用它。
ssh "$HOST" bash -s -- "$ACTION" "${ID:--}" "$DIR" <<'REMOTE'
set -euo pipefail
action="$1"; id="$2"; dir="$3"
cd "$dir"
key=ANIMEBOT_ADMIN_IDS

cur="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- | tr -d ' ')"
IFS=',' read -r -a ids <<< "${cur:-}"

write_back() {
  local joined
  joined="$(IFS=,; echo "${*-}")"
  if grep -qE "^${key}=" .env; then
    sed -i -E "s/^${key}=.*/${key}=${joined}/" .env
  else
    echo "${key}=${joined}" >> .env
  fi
}

case "$action" in
  list)
    n=${#ids[@]}
    [[ $n -eq 0 || -z "${ids[0]}" ]] && { echo "管理员（0）: （空）"; exit 0; }
    echo "管理员（$n）: $(IFS=、; echo "${ids[*]}")"
    exit 0
    ;;
  add)
    for x in "${ids[@]}"; do [[ "$x" == "$id" ]] && { echo "$id 已经是管理员了，没改"; exit 0; }; done
    if [[ -z "$cur" ]]; then write_back "$id"; else write_back "${ids[@]}" "$id"; fi
    ;;
  del)
    found=0
    for x in "${ids[@]}"; do [[ "$x" == "$id" ]] && found=1; done
    [[ $found -eq 1 ]] || { echo "$id 不在管理员名单里，没改"; exit 0; }
    if [[ ${#ids[@]} -le 1 ]]; then
      echo "拒绝：至少保留一个管理员 —— 全删了没人能进后台。" >&2
      echo "要换人：先 add 新的，再 del 旧的。" >&2
      exit 1
    fi
    left=()
    for x in "${ids[@]}"; do [[ "$x" != "$id" ]] && left+=("$x"); done
    write_back "${left[@]}"
    ;;
esac

systemctl restart animebot
echo "✅ .env 已更新为 ${key}=$(grep -E "^${key}=" .env | cut -d= -f2-)，animebot 已重启生效"
REMOTE
