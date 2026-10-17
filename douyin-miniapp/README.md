# 智旅灵犀抖音小程序端

这是 `SmartTourism` 项目的抖音小程序用户端，用于承接抖音短视频挂载后的游客访问场景。

## 已实现页面

- 首页：展示产品定位和核心入口
- AI 咨询：调用现有 Flask 后端 `/api/chat`
- 行程规划：调用现有 Flask 后端 `/api/plan`
- 社区攻略：本地保存用户攻略、点击排行、收藏排行，并调用 `/api/social/hotspots` 展示官方热点

## 上传前必须配置

打开 `utils/config.js`，把：

```js
API_BASE_URL: "https://api.lingxiwenlv.icu"
```

替换成已经部署好的公网 HTTPS 后端域名，例如：

```js
API_BASE_URL: "https://api.lingxiwenlv.icu"
```

注意：

- 正式提审前，后端必须是 `https://`。
- 该域名必须在抖音开放平台后台配置为“安全域名”。
- 本项目不能直接使用 `http://localhost:5000` 提审。

## 后端对应接口

小程序端复用现有 Flask 服务：

```text
POST /api/chat
POST /api/plan
GET  /api/social/hotspots
```

如果要展示真实抖音/小红书热点，需要在后端配置官方或授权接口环境变量：

```text
DOUYIN_HOTSPOT_ENDPOINT
DOUYIN_ACCESS_TOKEN
XHS_HOTSPOT_ENDPOINT
XHS_ACCESS_TOKEN
```

## 发布流程

1. 用抖音开发者工具打开 `douyin-miniapp` 目录。
2. 确认 AppID 为 `ttb5fec3432b845b4d01`。
3. 在开发者工具中预览并扫码自测。
4. 点击“上传”，填写版本号和更新日志。
5. 到抖音开放平台控制台进入“开发版本管理”。
6. 对测试版本扫码体验，确认核心流程可用。
7. 提交审核，选择上线 App。
8. 审核通过后发布上线。

## 与短视频挂载的关系

当前小程序需要先完成：

- 小程序发布上线
- 设置并通过主营类目审核

完成后，回到“短视频自主挂载能力”页面，才能继续申请开通挂载。

## 建议主营类目

请以抖音开放平台后台“已通过资质审核的服务类目”为准。结合当前项目，优先考虑文旅、旅游服务、景区服务、本地生活服务、旅游攻略等方向。
