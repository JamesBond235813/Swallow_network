---
name: vpn-deploy-xui-nginx
description: >
  在一台全新的 Debian/Ubuntu VPS 上一键部署 VLESS + XHTTP + TLS + Cloudflare CDN 的自建 VPN
  节点（Nginx 反代 + 3X-UI 面板 + acme.sh 证书）。当用户要求：搭梯子 / 科学上网 / 部署 VPN /
  自建代理 / x-ui / 3x-ui / VLESS / 绕过 GFW，或在新服务器复制已有 VPN 节点时使用。
  Deploy a self-hosted VLESS+XHTTP+TLS VPN behind Cloudflare on a fresh Debian/Ubuntu VPS.
allowed-tools: Bash, Read, Write
---

# 一键部署 VPN 节点（VLESS + XHTTP + TLS + Cloudflare CDN / Nginx 版）

> 本 runbook 由一次真实部署（GCP + Ubuntu 26.04 + 新版 3X-UI v3.3.x）提炼而成，已把
> 原始教程没覆盖的坑全部修正：新版 3X-UI 交互式安装器、inbounds 表结构变化、幂等 SQL、
> Ubuntu 新版无 /var/log/auth.log 的 fail2ban 适配、证书续期 reload。
>
> **执行模型**：本文所有 `bash` 块都在目标 VPS 上以 **root** 执行。若登录用户不是 root，
> 先 `sudo -i` 切到 root 再开始。逐块执行、每块检查输出，失败先排障再继续，不要跳步。

## 架构

```
客户端 → Cloudflare CDN (443, 橙云) → Nginx (TLS 反代) → Xray (127.0.0.1:10000) → Internet
         ↑ 隐藏真实 IP            ↑ 伪装站 + TLS 终止     ↑ 代理核心（3X-UI 管理）
```

---

## 第一步：收集变量（必须先填全）

向用户收集以下信息，**逐轮**询问（服务器 → 域名 → Cloudflare），全部拿到再开始：

| 变量 | 含义 | 示例 |
|------|------|------|
| `SERVER_IP` | VPS 公网 IPv4 | `203.0.113.50` |
| `SSH_USER` / `SSH_PORT` | SSH 用户 / 端口 | `root` / `22` |
| SSH 认证 | 密码或密钥（密钥给本地路径） | `~/.ssh/id_ed25519` |
| `ROOT_DOMAIN` | **已托管在 Cloudflare 的域名**（NS 已指向 CF、状态 Active） | `example.dpdns.org` |
| `SUBDOMAIN_PREFIX` | 子域前缀（节点名） | `node1` |
| `EMAIL` | 证书注册邮箱 | `you@gmail.com` |
| CF 认证方式 | Global API Key 或 API Token | 二选一 |
| `CF_Key`+`CF_Email`（或 `CF_Token`） | CF 凭据（Key 方式需 **CF 账户邮箱**） | — |

前置条件（不满足先解决）：
- VPS 为 **Debian 11/12 或 Ubuntu 20.04+**，且 **80/443 端口空闲**（见第二步预检）。
- 域名已在 Cloudflare、状态 **Active**。
- 已拿到 Cloudflare API 凭据；Token 方式需权限 `Zone-DNS-Edit`、`Zone-SSL-Edit`、`Zone-Settings-Edit`。

> ⚠️ Global API Key 方式里的邮箱填 **CF 账户注册邮箱**（面板右上角头像下确认），可能与证书邮箱不同；填错会报 `Unknown X-Auth-Key or X-Auth-Email`。

收集完用占位符模板向用户复述确认，全部来自用户输入，不要用示例值。

---

## 第二步：定义变量 + 预检（步骤 0、1）

```bash
# ===== 用户提供的值（替换为实际值）=====
ROOT_DOMAIN="example.dpdns.org"
SUBDOMAIN_PREFIX="node1"
EMAIL="you@gmail.com"
SSH_PORT="22"
# Cloudflare 认证（二选一）
CF_Key="your_global_api_key"; CF_Email="your_cf_account_email"
# CF_Token="your_api_token"
# ===== 自动拼接 =====
DOMAIN="${SUBDOMAIN_PREFIX}.${ROOT_DOMAIN}"
XUI_PORT="54321"
echo "DOMAIN=$DOMAIN"

# ===== 预检 =====
whoami                                   # 必须 root（否则先 sudo -i）
cat /etc/os-release | head -2            # 必须 Debian/Ubuntu
ss -tlnp | grep -E ':80 |:443 ' && echo "!! 80/443 被占用，见下方说明" || echo "80/443 空闲 OK"
```

> **若 80/443 被占用**：说明机器上已有 Web 服务（Nginx/Apache/Caddy）。本 Nginx 方案需要独占
> 80/443。两种处理：① 停掉占用者（`systemctl stop <svc>`）后继续；② 若占用者是你在用的
> Caddy 且不能停，则改用「Caddy 共存方案」（用 Caddy 加站点块反代 Xray、证书仍用 acme.sh
> 装到 `/etc/caddy/certs/` 并 chown caddy）。本文走 Nginx 独占路径。

---

## 第三步：安装依赖（步骤 2）

```bash
apt update && apt install -y nginx ufw fail2ban curl wget openssl sqlite3 socat cron python3-bcrypt
# 若 python3-bcrypt 不存在：pip3 install bcrypt --break-system-packages
```

---

## 第四步：生成安全参数（步骤 3）

```bash
mkdir -p /root/.secrets && chmod 700 /root/.secrets
openssl rand -hex 8 > /root/.secrets/ws_path.txt                       # XHTTP 路径
cat /proc/sys/kernel/random/uuid > /root/.secrets/vless_uuid.txt       # 客户端 UUID
echo "admin_$(openssl rand -hex 4)" > /root/.secrets/xui_username.txt  # 面板用户名
openssl rand -base64 18 > /root/.secrets/xui_password.txt              # 面板密码
DECOY_ADJS=(Alpine Summit Ridge Vertex Meridian Cascade Atlas Zenith Stratum Lumina Crestwood Northwind Silverline Boreal Solstice)
DECOY_NOUNS=(Systems Solutions Labs Partners Group Dynamics Ventures Networks Analytics Consulting)
echo "${DECOY_ADJS[RANDOM % ${#DECOY_ADJS[@]}]} ${DECOY_NOUNS[RANDOM % ${#DECOY_NOUNS[@]}]}" > /root/.secrets/decoy_name.txt
chmod 600 /root/.secrets/*.txt

WS_PATH=$(cat /root/.secrets/ws_path.txt); UUID=$(cat /root/.secrets/vless_uuid.txt)
XUI_USER=$(cat /root/.secrets/xui_username.txt); XUI_PASS=$(cat /root/.secrets/xui_password.txt)
DECOY_NAME=$(cat /root/.secrets/decoy_name.txt)
echo "WS_PATH=$WS_PATH UUID=$UUID XUI_USER=$XUI_USER XUI_PASS=$XUI_PASS DECOY=$DECOY_NAME"
```

---

## 第五步：申请 SSL 证书（步骤 4，acme.sh + Cloudflare DNS）

```bash
export CF_Key CF_Email      # Token 方式改为：export CF_Token
curl https://get.acme.sh | sh -s email="$EMAIL"
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt
~/.acme.sh/acme.sh --issue --server letsencrypt -d "$ROOT_DOMAIN" -d "*.$ROOT_DOMAIN" --dns dns_cf --keylength ec-256
mkdir -p /root/cert
~/.acme.sh/acme.sh --install-cert -d "$ROOT_DOMAIN" --ecc \
  --key-file       "/root/cert/$ROOT_DOMAIN.key" \
  --fullchain-file /root/cert/fullchain.cer \
  --reloadcmd      "systemctl reload nginx"
chmod 600 /root/cert/*.key
ls -l /root/cert/
```

> 成功标志：`/root/cert/` 下有 `fullchain.cer` 和 `$ROOT_DOMAIN.key`。
> 失败多为 CF 凭据不对或域名未 Active。`--ecc` 不能漏（证书是 ec-256，目录名带 `_ecc`）。

---

## 第六步：伪装站点（步骤 5）

```bash
mkdir -p "/var/www/$DOMAIN"
cat > "/var/www/$DOMAIN/index.html" << SITEEOF
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${DECOY_NAME}</title>
<meta name="description" content="Professional digital solutions and consulting services.">
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:#1a1a2e;line-height:1.6}.nav{background:#fff;padding:1rem 2rem;border-bottom:1px solid #e8e8e8;display:flex;justify-content:space-between;align-items:center}.nav-brand{font-size:1.3rem;font-weight:600;color:#2563eb;text-decoration:none}.hero{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:5rem 2rem;text-align:center}.hero h1{font-size:2.5rem;margin-bottom:1rem}.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2rem;padding:4rem 2rem;max-width:1100px;margin:0 auto}.feature{text-align:center;padding:2rem}.footer{background:#f8f9fa;padding:2rem;text-align:center;color:#888;font-size:.85rem}</style></head>
<body><nav class="nav"><a href="#" class="nav-brand">${DECOY_NAME}</a></nav>
<section class="hero"><h1>Digital Solutions for Modern Business</h1><p>We help companies transform their operations with cutting-edge technology.</p></section>
<section class="features"><div class="feature"><h3>Cloud Infrastructure</h3><p>Scalable and secure cloud solutions.</p></div><div class="feature"><h3>Process Automation</h3><p>Streamline workflows and reduce costs.</p></div><div class="feature"><h3>Data Analytics</h3><p>Turn data into actionable insights.</p></div></section>
<footer class="footer">&copy; 2026 ${DECOY_NAME}. All rights reserved.</footer></body></html>
SITEEOF
```

---

## 第七步：Nginx 配置（步骤 6、7）

```bash
# 全局加固
sed -i 's/ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;/ssl_protocols TLSv1.2 TLSv1.3;/' /etc/nginx/nginx.conf
sed -i 's/# server_tokens off;/server_tokens off;/' /etc/nginx/nginx.conf

# 站点配置（heredoc 不带引号：$DOMAIN/$ROOT_DOMAIN/$WS_PATH 由 bash 展开；
# Nginx 自身变量用 \$ 转义保留）
cat > "/etc/nginx/sites-available/$DOMAIN" << NGINXEOF
server {
    listen 80; listen [::]:80; server_name $DOMAIN;
    return 301 https://\$server_name\$request_uri;
}
server {
    listen 443 ssl http2; listen [::]:443 ssl http2; server_name $DOMAIN;
    ssl_certificate     /root/cert/fullchain.cer;
    ssl_certificate_key /root/cert/$ROOT_DOMAIN.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305';
    ssl_prefer_server_ciphers on;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    server_tokens off;
    root /var/www/$DOMAIN; index index.html;

    location /$WS_PATH {
        proxy_pass http://127.0.0.1:10000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 86400s; proxy_send_timeout 86400s;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off; proxy_cache off;
    }
    location / { try_files \$uri \$uri/ =404; }
    location ~ /\. { deny all; }
}
NGINXEOF

ln -sf "/etc/nginx/sites-available/$DOMAIN" /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

---

## 第八步：防火墙 UFW（步骤 8）

```bash
ufw default deny incoming; ufw default allow outgoing
ufw allow "$SSH_PORT/tcp"; ufw allow 80/tcp; ufw allow 443/tcp
ufw deny "$XUI_PORT/tcp"
echo "y" | ufw enable
ufw status verbose
```

> 先放行 SSH 端口再 enable，不会断开当前连接。云厂商若另有安全组（GCP VPC / AWS SG），
> 还需在**云控制台**放行 80/443（见末尾「云防火墙」）。

---

## 第九步：Fail2Ban（步骤 9，systemd 后端兼容新版无 auth.log）

```bash
cat > /etc/fail2ban/jail.local << JAILEOF
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5
banaction = ufw

[sshd]
enabled = true
port = $SSH_PORT
backend = systemd
maxretry = 3
bantime = 7200
JAILEOF
systemctl enable fail2ban && systemctl restart fail2ban
fail2ban-client status sshd
```

---

## 第十步：安装 3X-UI（步骤 10，**交互式，新版有 SSL 提问**）

```bash
bash <(curl -Ls https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh)
```

新版（v3.3.x）安装器的交互，**务必全部答完、不要中途 Ctrl+C**（中断会导致没创建 systemd 服务）：

| 提问 | 回答 |
|------|------|
| 已安装？是否重装/更新 | 重装 / yes |
| Database Selection | `1`（SQLite） |
| customize Panel Port | `y`，端口填 `54321` |
| 用户名 / 密码 | 随便（如 `temp`/`temp`，下一步覆盖） |
| **SSL Certificate Setup** | `4`（**Skip SSL** —— 面板走 SSH 隧道，不需自己的证书，否则会抢 80 端口） |
| Bind the panel to 127.0.0.1 only? | **`y`** |
| 是否现在启动 | `y` |

装完验证（关键：必须有 service 文件）：

```bash
ls -l /etc/systemd/system/x-ui.service 2>/dev/null || ls -l /usr/lib/systemd/system/x-ui.service
systemctl is-active x-ui ; ls -la /etc/x-ui/x-ui.db
```

---

## 第十一步：用数据库覆盖面板配置（步骤 11、12，幂等）

```bash
DB=/etc/x-ui/x-ui.db
XUI_PORT="54321"
XUI_USER=$(cat /root/.secrets/xui_username.txt); XUI_PASS=$(cat /root/.secrets/xui_password.txt)
systemctl stop x-ui

# settings 用「先删后插」保证每个 key 唯一（兼容 key 无唯一约束的情况）
set_kv(){ sqlite3 "$DB" "DELETE FROM settings WHERE key='$1'; INSERT INTO settings (key,value) VALUES ('$1','$2');"; }
set_kv webListen 127.0.0.1
set_kv webPort "$XUI_PORT"
set_kv subEnable false
set_kv subListen 127.0.0.1
set_kv secret "$(openssl rand -hex 16)"

# 面板强密码（bcrypt）
HASHED=$(python3 -c "import bcrypt; print(bcrypt.hashpw('$XUI_PASS'.encode(), bcrypt.gensalt(10)).decode())")
sqlite3 "$DB" "UPDATE users SET username='$XUI_USER', password='$HASHED' WHERE id=1;"

# 完整 Xray 模板（必须含 outbounds，否则连上不能上网）
cat > /tmp/xray_tpl.json << 'JSONEOF'
{
  "log": {"access":"none","dnsLog":false,"loglevel":"warning"},
  "dns": {"servers":["8.8.8.8","1.1.1.1"]},
  "routing": {"domainStrategy":"IPIfNonMatch","rules":[
    {"type":"field","inboundTag":["api"],"outboundTag":"api"},
    {"type":"field","outboundTag":"blocked","protocol":["bittorrent"]}]},
  "outbounds": [
    {"tag":"direct","protocol":"freedom","settings":{}},
    {"tag":"blocked","protocol":"blackhole","settings":{}}],
  "policy": {"levels":{"0":{"statsUserDownlink":true,"statsUserUplink":true}},"system":{"statsInboundDownlink":true,"statsInboundUplink":true}},
  "api": {"tag":"api","services":["HandlerService","LoggerService","StatsService"]},
  "stats": {}
}
JSONEOF
TPL=$(cat /tmp/xray_tpl.json); rm -f /tmp/xray_tpl.json
sqlite3 "$DB" "DELETE FROM settings WHERE key='xrayTemplateConfig';"
sqlite3 "$DB" "INSERT INTO settings (key,value) VALUES ('xrayTemplateConfig','$TPL');"

# 记下面板路径（登录要带）
echo "webBasePath = $(sqlite3 "$DB" "SELECT value FROM settings WHERE key='webBasePath';")"
```

---

## 第十二步：创建 VLESS 入站（步骤 13，**先查表结构再插**）

> ⚠️ 新版 3X-UI 的 `inbounds` 表字段和旧教程不同（**没有 `all_time`**，多了 `node_id`、
> `sub_sort_index` 等）。所有列允许为空，因此**只插入必要列**即可，切勿照搬旧 INSERT。

```bash
DB=/etc/x-ui/x-ui.db
UUID=$(cat /root/.secrets/vless_uuid.txt); WS_PATH=$(cat /root/.secrets/ws_path.txt)
ROOT_DOMAIN="example.dpdns.org"; SUBDOMAIN_PREFIX="node1"; DOMAIN="$SUBDOMAIN_PREFIX.$ROOT_DOMAIN"

# （可选）先看一眼表结构，确认列名
sqlite3 "$DB" "PRAGMA table_info(inbounds);"

SETTINGS="{\"clients\":[{\"id\":\"$UUID\",\"flow\":\"\",\"email\":\"default-user\",\"limitIp\":0,\"totalGB\":0,\"expiryTime\":0,\"enable\":true,\"tgId\":\"\",\"subId\":\"\",\"reset\":0}],\"decryption\":\"none\",\"fallbacks\":[]}"
STREAM="{\"network\":\"xhttp\",\"security\":\"none\",\"xhttpSettings\":{\"path\":\"/$WS_PATH\",\"host\":\"$DOMAIN\",\"mode\":\"auto\"}}"
SNIFFING="{\"enabled\":true,\"destOverride\":[\"http\",\"tls\",\"quic\"],\"metadataOnly\":false,\"routeOnly\":true}"

sqlite3 "$DB" "DELETE FROM inbounds WHERE tag='inbound-10000';"
sqlite3 "$DB" "INSERT INTO inbounds (user_id,up,down,total,remark,enable,expiry_time,listen,port,protocol,settings,stream_settings,tag,sniffing) VALUES (1,0,0,0,'VLESS-XHTTP-TLS-CF',1,0,'127.0.0.1',10000,'vless','$SETTINGS','$STREAM','inbound-10000','$SNIFFING');"

systemctl enable x-ui >/dev/null 2>&1; systemctl restart x-ui; sleep 5
```

---

## 第十三步：健康检查（步骤 14）

```bash
DB=/etc/x-ui/x-ui.db; WS_PATH=$(cat /root/.secrets/ws_path.txt)
ROOT_DOMAIN="example.dpdns.org"; SUBDOMAIN_PREFIX="node1"; DOMAIN="$SUBDOMAIN_PREFIX.$ROOT_DOMAIN"
echo "--- 服务 ---"; systemctl is-active nginx x-ui fail2ban
echo "--- 端口（10000/54321 应在 127.0.0.1）---"; ss -tlnp | grep -E ':80 |:443 |:10000 |:54321 '
echo "--- Xray 配置 ---"; CFG=""; for f in /usr/local/x-ui/bin/config.json /etc/x-ui/bin/config.json; do [ -f "$f" ] && CFG="$f"; done
python3 -c "import json;c=json.load(open('$CFG'));print('Outbounds:',[o.get('protocol') for o in c.get('outbounds',[])]);print('Inbound ports:',[i.get('port') for i in c.get('inbounds',[])])"
echo "--- 本地 Xray（非000/502即在响应）---"; curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1:10000/$WS_PATH"
echo "--- 经 Nginx（非502即回源成功；404/400 都算通）---"; curl -sk -o /dev/null -w '%{http_code}\n' --resolve "$DOMAIN:443:127.0.0.1" -X POST "https://$DOMAIN/$WS_PATH"
```

全绿标准：`nginx`/`x-ui` active；`10000`/`54321` 监听在 `127.0.0.1`；Outbounds 含 `freedom`+`blackhole`；Inbound ports 含 `10000`；两条 curl 都不是 `502/000`。

---

## 第十四步：生成链接 + 保存配置（步骤 15）

```bash
UUID=$(cat /root/.secrets/vless_uuid.txt); WS_PATH=$(cat /root/.secrets/ws_path.txt)
XUI_USER=$(cat /root/.secrets/xui_username.txt); XUI_PASS=$(cat /root/.secrets/xui_password.txt)
ROOT_DOMAIN="example.dpdns.org"; SUBDOMAIN_PREFIX="node1"; DOMAIN="$SUBDOMAIN_PREFIX.$ROOT_DOMAIN"
SERVER_IP=$(curl -s --max-time 10 ifconfig.me)
BASE=$(sqlite3 /etc/x-ui/x-ui.db "SELECT value FROM settings WHERE key='webBasePath';")
LINK="vless://${UUID}@${DOMAIN}:443?encryption=none&security=tls&sni=${DOMAIN}&type=xhttp&host=${DOMAIN}&path=%2F${WS_PATH}&fp=chrome#VLESS-${SUBDOMAIN_PREFIX}"
cat > /root/vpn-config.txt << EOF
VLESS link (one line!):
$LINK

Panel via SSH tunnel:
  ssh -L 2222:127.0.0.1:54321 ${SSH_USER:-root}@${SERVER_IP}
  URL: http://localhost:2222${BASE}
  user: $XUI_USER   pass: $XUI_PASS
EOF
chmod 600 /root/vpn-config.txt; cat /root/vpn-config.txt
```

---

## 第十五步：Cloudflare 控制台（部署后必做）

1. **DNS → Records → Add**：`A`，名称 `SUBDOMAIN_PREFIX`，内容 = 服务器 IP，**代理状态橙云(Proxied)**。
2. **SSL/TLS → Overview**：加密模式 **Full (strict)**。
3. **SSL/TLS → Edge Certificates**：Minimum TLS Version = **1.2**。
4. **Network**：WebSockets 开启（默认开）。

等 1–2 分钟生效，浏览器访问 `https://<DOMAIN>` 应看到伪装站；客户端导入 VLESS 链接即可。

---

## 客户端 & 运维

- **链接格式**（端口必须 443、必须有 `security=tls`/`sni`/`fp=chrome`；**面板导出的链接参数是错的，别用**）：
  `vless://<UUID>@<DOMAIN>:443?encryption=none&security=tls&sni=<DOMAIN>&type=xhttp&host=<DOMAIN>&path=%2F<WS_PATH>&fp=chrome#<备注>`
- **客户端**：iOS Shadowrocket/V2Box；Android v2rayNG；Win v2rayN/Clash Verge；mac V2RayXS/Clash Verge。
- **加用户**：SSH 隧道进面板 → 现有入站「添加客户端」（**不要新建入站**）→ 用上面模板拼链接。
- **证书续期**：acme.sh 自动（每 ~60 天）并 reload nginx，无需手动。
- **查流量**：`sqlite3 /etc/x-ui/x-ui.db "SELECT remark,up,down FROM inbounds;"`

## 排障要点

1. 连上不能上网 → `xrayTemplateConfig` 缺 `outbounds`（重灌第十一步模板）。
2. 经 Nginx 502 → Xray 没起：`systemctl restart x-ui`，查 `ss -tlnp | grep 10000`。
3. 客户端连不上但浏览器能开伪装站 → 八成链接复制断行 / 缺参数。
4. 改完面板配置不同步 → `systemctl restart x-ui` 强制重生成 `config.json`。
5. 装到一半没有 `x-ui.service` → 安装器被 Ctrl+C 了，重跑并答完所有交互（尤其最后 bind 选 y）。

## 可选：云防火墙（安全组）

- **GCP**：VPC 防火墙放行 `tcp:80,443`（给实例打 `http-server`/`https-server` 标记，或建 `0.0.0.0/0` 入站规则）。22 通常默认开。
- **AWS/阿里云/腾讯云**：在安全组放行 80/443 入站。

## ⚠️ 成本提醒（GCP 免费机 + 中国使用）

GCP Always Free 出站**每月仅 1 GB，且明确排除中国/澳大利亚**——到中国的出站不免费。
即便走 Cloudflare（GCP 实际计费目的地是 CF 边缘），免费额度也极小，正常使用会很快产生
按量费用。**务必在云控制台设预算告警**，并可在 x-ui 给客户端设 `totalGB` 月度上限兜底。
若用免费域名（如 dpdns.org），记得**每 180 天续期**。

---

### 给 Codex 的执行提示
- 本文 `bash` 块按顺序在目标 VPS 以 root 执行；非 root 先 `sudo -i`。
- **第十步是交互式**，必须按表格逐项回答、答完，不能用非交互方式跳过，也不要 Ctrl+C。
- 每块执行后检查输出，异常先按「排障要点」处理再继续。
- 所有占位符（`ROOT_DOMAIN`、`EMAIL`、`CF_Key` 等）替换为用户实际值后再执行。
