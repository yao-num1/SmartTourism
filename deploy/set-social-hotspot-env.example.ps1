# 智旅灵犀 · 抖音/小红书热点接口环境变量配置模板
# 使用方式：
# 1. 将本文件复制为 set-social-hotspot-env.ps1
# 2. 把下面的 PLACEHOLDER 替换成你从 TikHub、官方平台或授权服务商拿到的真实接口地址和 Token
# 3. 在 PowerShell 中运行：.\deploy\set-social-hotspot-env.ps1
# 4. 重启后端服务，让 Flask 进程读取新的环境变量
#
# 注意：
# - 不要把填好密钥的 set-social-hotspot-env.ps1 提交到代码仓库或发给他人
# - 优先支持 TikHub 抖音热点接口，也兼容抖音和小红书官方/授权 JSON 接口，不做网页抓取
# - 接口返回会在 server/social_data.py 中归一化为 title、summary、url、author、cover、heat 等字段

# ========== TikHub 抖音热点接口（推荐） ==========
# 在 https://user.tikhub.io/dashboard/api 获取 API Key。
# 配置后后端会优先调用 TikHub 抖音榜单接口，失败时自动回退到本地市场趋势包。
[Environment]::SetEnvironmentVariable(
  'TIKHUB_API_KEY',
  '替换为你的 TikHub API Key',
  'User'
)

# 可选：中国大陆用户请使用加速域名 api.tikhub.dev（主域名 api.tikhub.io 已被 GFW 屏蔽）。
[Environment]::SetEnvironmentVariable(
  'TIKHUB_DOUYIN_BASE_URL',
  'https://api.tikhub.dev',
  'User'
)

# 可选：自定义要调用的 TikHub 抖音接口，多个接口用英文逗号分隔。
# 留空时默认调用搜索热榜、话题热榜、内容热词、热门话题、热门视频和城市热榜。
[Environment]::SetEnvironmentVariable(
  'TIKHUB_DOUYIN_ENDPOINTS',
  '',
  'User'
)

# ========== 抖音官方/授权热点接口（兼容备用） ==========
[Environment]::SetEnvironmentVariable(
  'DOUYIN_HOTSPOT_ENDPOINT',
  '',
  'User'
)

# 二选一：如果你的接口使用 access_token，就填 DOUYIN_ACCESS_TOKEN
[Environment]::SetEnvironmentVariable(
  'DOUYIN_ACCESS_TOKEN',
  '',
  'User'
)

# 二选一：如果你的接口使用 client_token，就填 DOUYIN_CLIENT_TOKEN
[Environment]::SetEnvironmentVariable(
  'DOUYIN_CLIENT_TOKEN',
  '',
  'User'
)

# 可选：部分授权接口要求 client_key
[Environment]::SetEnvironmentVariable(
  'DOUYIN_CLIENT_KEY',
  '',
  'User'
)

# ========== 小红书官方/授权热点接口 ==========
[Environment]::SetEnvironmentVariable(
  'XHS_HOTSPOT_ENDPOINT',
  '',
  'User'
)

[Environment]::SetEnvironmentVariable(
  'XHS_ACCESS_TOKEN',
  '',
  'User'
)

# 可选：部分授权接口要求 app_id
[Environment]::SetEnvironmentVariable(
  'XHS_APP_ID',
  '',
  'User'
)

Write-Host '环境变量已写入当前 Windows 用户环境。请重启后端服务后验证。'
