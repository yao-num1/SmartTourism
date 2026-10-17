# 12 - SmartTourism UI 优化清单与实践计划

## 已完成 ✅

- [x] 暖土色系色彩系统（Earthy Warm）
- [x] 液态玻璃面板样式系统
- [x] Bento Grid 布局 CSS 框架
- [x] 响应式断点定义（480/640/768/1024/1280）
- [x] 打字指示器动画
- [x] Toast 通知系统
- [x] 暗色模式 CSS 变量体系
- [x] 多层级阴影系统（spatial depth）
- [x] 按钮系统（primary/accent/outline/sm/block）
- [x] 微交互过渡曲线定义

## 待优化 🔄

### Phase 1: 色彩与质感 (约1小时)
- [x] 桌面版样式已整合进 `dashboard/index.html`
- [ ] 统一 CSS 变量命名为 v3 标准
- [ ] 侧边栏改为液态玻璃全透明背景
- [ ] Header 导航栏改为 sticky + 玻璃态

### Phase 2: 布局重构 (约2小时)
- [ ] 首页欢迎区改为 Bento Grid 布局
- [ ] 对话气泡改用新的 spatial shadow
- [ ] 输入栏使用玻璃态背景
- [ ] 设置面板改为 Bento Card 排列
- [ ] 快速问题 chip 改为 pill shape

### Phase 3: 动效增强 (约1小时)
- [ ] 卡片 hover 添加 3D tilt 效果
- [ ] 页面切换按需添加过渡动画
- [ ] 消息插入添加 slide-in + fade 动画
- [ ] 发送按钮添加弹簧缩放
- [ ] 打字机光标闪烁效果

### Phase 4: 暗色模式+无障碍 (约1小时)
- [ ] 添加暗色模式一键切换按钮
- [ ] 所有 interactive 元素添加 focus-visible 环
- [ ] 检查文字对比度（WCAG AA 4.5:1）
- [ ] prefers-reduced-motion 媒体查询适配
- [ ] aria-label 补充到关键交互元素

### Phase 5: 移动端增强 (约1.5小时)
- [ ] 底部 Tab Bar 导航（对话/地图/设置）
- [ ] 所有触控目标 >= 44x44px
- [ ] safe-area-inset-bottom 底部安全区
- [ ] 输入框键盘弹出时自动上移
- [ ] 下拉刷新对话列表

## 内容增强

- [ ] 消息气泡添加操作按钮（复制/重新生成）
- [ ] AI 回复后自动推荐 3 个追问
- [ ] 地图页搜索结果改为底部抽屉 Panel
- [ ] 骨架屏 Loading 预置到卡片组件
- [ ] 移动端适配底部安全区

## 样式优化目标

| 指标 | 当前 | 目标 |
|------|------|------|
| CSS 变量覆盖率 | 60% | 95% |
| 动画帧率 | 30fps | 60fps (GPU加速) |
| 无障碍评分 | 未测 | WCAG AA |
| 暗色模式 | 无 | 一键切换 |
| 移动端适配 | 基础 | 全面优化 |
| 首次内容绘制 | 未测 | <1.5s |
