#!/bin/bash
# ============================================================
# SmartTourism 前端独立域名部署脚本
# 功能：配置 www.lingxiwenlv.icu + lingxiwenlv.icu 的 Nginx 和 SSL
# 用法：在服务器上执行 bash deploy/setup-frontend-domain.sh
# 前提：
#   1. 已在阿里云 DNS 添加 www 和 @ 的 A 记录指向 120.25.78.25
#   2. DNS 解析已生效（可用 dig www.lingxiwenlv.icu 验证）
#   3. 项目代码已上传到 /opt/smarttourism（含 dashboard 目录）
# ============================================================

set -e

PROJECT_DIR="/opt/smarttourism"
FRONTEND_DOMAINS="www.lingxiwenlv.icu lingxiwenlv.icu"
CERTBOT_EMAIL="admin@lingxiwenlv.icu"

echo "============================================"
echo "  SmartTourism 前端域名部署"
echo "  域名: www.lingxiwenlv.icu + lingxiwenlv.icu"
echo "============================================"

# 1. 检查 dashboard 目录
echo ""
echo "[1/5] 检查前端文件..."
if [ ! -f "$PROJECT_DIR/dashboard/index.html" ]; then
    echo "  [错误] $PROJECT_DIR/dashboard/index.html 不存在"
    echo "  请先上传 dashboard 目录到服务器"
    exit 1
fi
echo "  → 前端文件检查通过"

# 2. 检查 DNS 解析
echo ""
echo "[2/5] 检查 DNS 解析..."
for domain in $FRONTEND_DOMAINS; do
    IP=$(dig +short "$domain" 2>/dev/null | head -1)
    if [ -z "$IP" ]; then
        IP=$(nslookup "$domain" 2>/dev/null | grep -A1 "Name:" | grep "Address" | awk '{print $2}' | head -1)
    fi
    if [ "$IP" = "120.25.78.25" ] || [ "$IP" = "120.25.78.25." ]; then
        echo "  → $domain 解析正常 → $IP"
    else
        echo "  → [警告] $domain 未正确解析到 120.25.78.25（当前: ${IP:-未解析}）"
        echo "    请确认阿里云 DNS 已添加 A 记录并等待生效"
        read -p "  是否继续？(y/N): " CONTINUE
        if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
            echo "  已取消，请先配置 DNS 后重试"
            exit 0
        fi
    fi
done

# 3. 配置 Nginx
echo ""
echo "[3/5] 配置 Nginx..."
if [ ! -f "$PROJECT_DIR/deploy/nginx-frontend.conf" ]; then
    echo "  [错误] $PROJECT_DIR/deploy/nginx-frontend.conf 不存在"
    exit 1
fi
cp "$PROJECT_DIR/deploy/nginx-frontend.conf" /etc/nginx/sites-available/smarttourism-frontend
ln -sf /etc/nginx/sites-available/smarttourism-frontend /etc/nginx/sites-enabled/
echo "  → Nginx 前端配置已安装"

# 测试 Nginx 配置
if nginx -t; then
    echo "  → Nginx 配置语法检查通过"
    systemctl reload nginx
    echo "  → Nginx 已重载"
else
    echo "  → [错误] Nginx 配置语法有误，请检查"
    exit 1
fi

# 4. 申请 SSL 证书
echo ""
echo "[4/5] 申请 HTTPS 证书..."
if [ -d "/etc/letsencrypt/live/www.lingxiwenlv.icu" ]; then
    echo "  → 证书已存在，跳过申请"
else
    certbot --nginx -d www.lingxiwenlv.icu -d lingxiwenlv.icu \
        --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect || {
        echo "  → [警告] HTTPS 证书申请失败"
        echo "    可能原因：DNS 未生效、80 端口未放行、域名未备案"
        echo "    HTTP 服务仍可使用，稍后可手动重试："
        echo "    certbot --nginx -d www.lingxiwenlv.icu -d lingxiwenlv.icu"
    }
fi

# 5. 验证
echo ""
echo "[5/5] 验证服务..."
sleep 2

# 检查 HTTP
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://www.lingxiwenlv.icu/" 2>/dev/null || echo "000")
echo "  → HTTP 访问 www.lingxiwenlv.icu → 状态码: $HTTP_CODE"

# 检查 HTTPS（如果证书申请成功）
if [ -d "/etc/letsencrypt/live/www.lingxiwenlv.icu" ]; then
    HTTPS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://www.lingxiwenlv.icu/" 2>/dev/null || echo "000")
    echo "  → HTTPS 访问 www.lingxiwenlv.icu → 状态码: $HTTPS_CODE"

    HTTPS_API=$(curl -s -o /dev/null -w "%{http_code}" "https://www.lingxiwenlv.icu/api/health" 2>/dev/null || echo "000")
    echo "  → HTTPS API 健康检查 → 状态码: $HTTPS_API"
fi

echo ""
echo "============================================"
echo "  部署完成！"
echo "============================================"
echo ""
echo "访问地址："
if [ -d "/etc/letsencrypt/live/www.lingxiwenlv.icu" ]; then
    echo "  🔒 https://www.lingxiwenlv.icu/"
    echo "  🔒 https://lingxiwenlv.icu/"
else
    echo "  ⚠️  http://www.lingxiwenlv.icu/  (HTTP，证书待申请)"
    echo "  ⚠️  http://lingxiwenlv.icu/      (HTTP，证书待申请)"
fi
echo ""
echo "API 地址（保持不变）："
echo "  🔒 https://api.lingxiwenlv.icu/api/health"
echo ""
echo "常用命令："
echo "  查看 Nginx 日志: tail -f /var/log/nginx/access.log"
echo "  重载 Nginx: systemctl reload nginx"
echo "  续签证书: certbot renew"
