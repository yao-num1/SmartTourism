#!/bin/bash
# ============================================================
# SmartTourism 一键部署脚本（Ubuntu 22.04+）
# 用法：在服务器上执行 bash deploy.sh
# 前提：已通过 scp 上传项目到 /opt/smarttourism
# ============================================================

set -e

PROJECT_DIR="/opt/smarttourism"
VENV_DIR="$PROJECT_DIR/venv"
DOMAIN="api.lingxiwenlv.icu"
CERTBOT_EMAIL="admin@lingxiwenlv.icu"

echo "============================================"
echo "  SmartTourism 生产环境部署脚本"
echo "============================================"

# 1. 检查项目目录
if [ ! -d "$PROJECT_DIR" ]; then
    echo "[错误] 项目目录 $PROJECT_DIR 不存在"
    echo "请先通过 scp 上传项目代码"
    exit 1
fi

# 2. 安装系统依赖
echo "[1/7] 安装系统依赖..."
apt update -y
apt install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl

# 3. 创建虚拟环境
echo "[2/7] 创建 Python 虚拟环境..."
cd "$PROJECT_DIR"
python3 -m venv venv
source "$VENV_DIR/bin/activate"

# 4. 安装 Python 依赖
echo "[3/7] 安装 Python 依赖..."
pip install --upgrade pip
if [ -f "requirements-prod.txt" ]; then
    pip install -r requirements-prod.txt
else
    pip install flask flask-cors requests gunicorn beautifulsoup4
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    if [ -f "$PROJECT_DIR/deploy/.env.example" ]; then
        cp "$PROJECT_DIR/deploy/.env.example" "$PROJECT_DIR/.env"
    else
        touch "$PROJECT_DIR/.env"
    fi
fi

# 5. 配置 systemd 服务
echo "[4/7] 配置 systemd 服务..."
cp deploy/smarttourism.service /etc/systemd/system/smarttourism.service
systemctl daemon-reload
systemctl enable smarttourism
systemctl restart smarttourism
echo "  → smarttourism 服务状态：$(systemctl is-active smarttourism)"

# 6. 配置 Nginx
echo "[5/7] 配置 Nginx 反向代理..."
cp deploy/nginx-smarttourism.conf /etc/nginx/sites-available/smarttourism
ln -sf /etc/nginx/sites-available/smarttourism /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
echo "  → Nginx 服务状态：$(systemctl is-active nginx)"

# 7. 申请 HTTPS 证书
echo "[6/7] 申请 HTTPS 证书..."
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect || {
        echo "  → [警告] HTTPS 证书申请失败，HTTP 服务仍可用于排查。"
        echo "    请确认域名已备案、DNS 已解析到本机、80/443 端口已放行。"
    }
else
    echo "  → 证书已存在，跳过申请"
fi

# 8. 验证
echo "[7/7] 验证服务..."
sleep 2
HEALTH=$(curl -s http://127.0.0.1:5000/api/health 2>/dev/null || echo "FAIL")
if echo "$HEALTH" | grep -q "status"; then
    echo "  → 后端健康检查通过"
else
    echo "  → [警告] 后端健康检查未通过，请检查日志："
    echo "    journalctl -u smarttourism -f"
fi

echo ""
echo "============================================"
echo "  部署完成！"
echo "============================================"
echo ""
echo "下一步："
echo "  1. 如需配置大模型或高德 Key：nano $PROJECT_DIR/.env"
echo "  2. 在抖音开放平台配置安全域名：https://$DOMAIN"
echo "  3. 重启服务：systemctl restart smarttourism"
echo ""
echo "常用命令："
echo "  查看日志：journalctl -u smarttourism -f"
echo "  重启后端：systemctl restart smarttourism"
echo "  重启 Nginx：systemctl restart nginx"
