#!/bin/bash
# ============================================================
# SmartTourism 前端独立域名一键配置脚本
# 直接在服务器 SSH 终端粘贴执行即可，无需上传文件
# ============================================================

set -e

echo "============================================"
echo "  SmartTourism 前端域名部署"
echo "  www.lingxiwenlv.icu + lingxiwenlv.icu"
echo "============================================"

# 1. 检查前端文件
echo ""
echo "[1/5] 检查前端文件..."
if [ ! -f "/opt/smarttourism/dashboard/index.html" ]; then
    echo "  [错误] /opt/smarttourism/dashboard/index.html 不存在"
    exit 1
fi
echo "  OK"

# 2. 写入 Nginx 前端配置
echo ""
echo "[2/5] 写入 Nginx 配置..."
cat > /etc/nginx/sites-available/smarttourism-frontend << 'NGINX_EOF'
server {
    listen 80;
    server_name www.lingxiwenlv.icu lingxiwenlv.icu;

    root /opt/smarttourism/dashboard;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 180s;
    }

    location /map {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /m {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location ~* \.(jpg|jpeg|png|gif|ico|svg|webp|css|js|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
NGINX_EOF

ln -sf /etc/nginx/sites-available/smarttourism-frontend /etc/nginx/sites-enabled/
echo "  OK"

# 3. 检查 Nginx 配置并重载
echo ""
echo "[3/5] 检查并重载 Nginx..."
nginx -t && systemctl reload nginx
echo "  OK"

# 4. 申请 SSL 证书
echo ""
echo "[4/5] 申请 HTTPS 证书..."
if [ -d "/etc/letsencrypt/live/www.lingxiwenlv.icu" ]; then
    echo "  证书已存在，跳过"
else
    certbot --nginx -d www.lingxiwenlv.icu -d lingxiwenlv.icu \
        --non-interactive --agree-tos -m admin@lingxiwenlv.icu --redirect || {
        echo ""
        echo "  [警告] 证书申请失败"
        echo "  可能原因：DNS 尚未生效，请等待 5 分钟后重试："
        echo "  certbot --nginx -d www.lingxiwenlv.icu -d lingxiwenlv.icu --non-interactive --agree-tos -m admin@lingxiwenlv.icu --redirect"
    }
fi

# 5. 验证
echo ""
echo "[5/5] 验证..."
sleep 2
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://www.lingxiwenlv.icu/ 2>/dev/null || echo "000")
echo "  HTTP  www.lingxiwenlv.icu  → $HTTP"

if [ -d "/etc/letsencrypt/live/www.lingxiwenlv.icu" ]; then
    HTTPS=$(curl -s -o /dev/null -w "%{http_code}" https://www.lingxiwenlv.icu/ 2>/dev/null || echo "000")
    echo "  HTTPS www.lingxiwenlv.icu  → $HTTPS"
    API=$(curl -s -o /dev/null -w "%{http_code}" https://www.lingxiwenlv.icu/api/health 2>/dev/null || echo "000")
    echo "  API   /api/health         → $API"
fi

echo ""
echo "============================================"
echo "  完成！"
echo "============================================"
