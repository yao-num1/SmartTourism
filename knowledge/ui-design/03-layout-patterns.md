# 03 - 布局模式与空间结构

## 一、2025-2026 爆款布局模式

### 1.1 Bento Grid（便当盒网格）⭐ TOP 1

**核心规则**：
- Grid 容器：`grid-template-columns: repeat(4, 1fr)`
- 卡片尺寸多样化（1x, 2x, 4x span）
- 圆角统一 16-20px
- Gap 固定 12-16px
- 卡片内 padding 20-28px

```css
.bento-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.bento-card {
  border-radius: 20px;
  padding: 24px;
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
  transition: box-shadow var(--transition-normal);
}

.bento-card:hover {
  box-shadow: var(--shadow-lg);
}

.bento-card.span-2 { grid-column: span 2; }
.bento-card.span-3 { grid-column: span 3; }
.bento-card.span-4 { grid-column: span 4; }
.bento-card.span-row-2 { grid-row: span 2; }

@media (max-width: 768px) {
  .bento-grid { grid-template-columns: repeat(2, 1fr); }
  .bento-card.span-2, .bento-card.span-3, .bento-card.span-4 { grid-column: span 2; }
}
```

**适用**：SmartTourism 首页 / 仪表盘改造

---

### 1.2 Sticky Scroll Sections（粘性滚动分节）

Apple 产品页风格：每个内容区在滚动时粘在顶部，制造沉浸感。

```css
.sticky-section {
  position: sticky;
  top: 0;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
```

**适用**：SmartTourism 营销落地页

---

### 1.3 Split Screen（分屏布局）

左右分屏，左侧固定品牌/导航，右侧滚动内容。

```
┌──────────┬──────────────────────┐
│          │                      │
│  Logo    │    Content Area      │
│  Nav     │    (Scrollable)      │
│  Brand   │                      │
│          │                      │
└──────────┴──────────────────────┘
   40%             60%
```

**桌面版 SmartTourism 当前结构与此类似** ✅

---

### 1.4 Full-Viewport Hero（全屏首屏）

```css
.hero {
  min-height: 100vh;
  min-height: 100dvh; /* dynamic viewport height */
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-8);
}
```

---

### 1.5 Card Grid with Masonry（瀑布流卡片）

Pinterest 风格，适合营销内容/图文混排。

```css
.masonry {
  columns: 3 300px;
  column-gap: 16px;
}
.masonry-item {
  break-inside: avoid;
  margin-bottom: 16px;
}
```

---

## 二、响应式断点策略

```css
/* 2025 推荐断点 */
/* 小手机 */   @media (max-width: 480px) { }
/* 大手机 */   @media (max-width: 640px) { }
/* 平板竖屏 */ @media (max-width: 768px) { }
/* 平板横屏 */ @media (max-width: 1024px) { }
/* 笔记本 */   @media (max-width: 1280px) { }
/* 桌面 */     @media (min-width: 1281px) { }
```

---

## 三、导航模式对比

| 模式 | 适用 | 优点 | 缺点 |
|------|------|------|------|
| 底部 Tab Bar | 3-5 个顶级页面 | 拇指触达 | 最多5项 |
| 汉堡菜单 | 复杂应用 | 空间节省 | 发现性差 |
| 顶部 Tab | 少数页面切换 | 可见性高 | 占空间 |
| 命令面板 (⌘K) | 工具型应用 | 功能无边 | 需要教育用户 |
| 浮动操作栏 | 核心操作 | 引导性强 | 遮挡内容 |

**SmartTourism 推荐**：桌面端侧边栏 + 移动端底部 Tab Bar ✅

---

## 四、间距系统（8px Soft Grid）

```css
/* 所有间距为 4px 的倍数 */
.gap-xs  { gap: 4px; }   /* 紧密关联 */
.gap-sm  { gap: 8px; }   /* 相关元素 */
.gap-md  { gap: 16px; }  /* 默认间距 */
.gap-lg  { gap: 24px; }  /* 分区 */
.gap-xl  { gap: 32px; }  /* 大分区 */
.gap-2xl { gap: 48px; }  /* 章节 */
```

---

## 五、容器策略

```css
/* 推荐：居中 + 最大宽度 */
.container {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px; /* 16px on mobile */
}

@media (max-width: 640px) {
  .container { padding: 0 16px; }
}
```
