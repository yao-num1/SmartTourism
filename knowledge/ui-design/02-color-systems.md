# 02 - 色彩系统与配色方案库

## 一、2025-2026 爆款配色方向

### 1.1 暖土色系（Earthy Warm）⭐ 文旅行业首选

```
Primary:    #2D5A27  (森林绿)
Secondary:  #C8944A  (古铜金)
Accent:     #E07B3C  (陶土橙)
Bg:         #F5F0EB  (羊皮纸)
Surface:    #FFFFFF
Text:       #2C2C2C
Muted:      #999999
```

**心理学**：自然、温暖、可信赖 —— 完美契合文旅行业

### 1.2 暗色系专业版（Dark Professional）

适合 AI/技术产品：

```
Primary:    #7C3AED  (紫罗兰)
Secondary:  #06B6D4  (青蓝)
Bg:         #0F0F23  (深蓝黑)
Surface:    #1A1A2E  
Card:       #252542
Text:       #E2E8F0
Muted:      #94A3B8
Border:     rgba(255,255,255,0.06)
```

### 1.3 极简黑白（Minimal Mono）

```
Primary:    #1A1A1A
Bg:         #FAFAFA
Surface:    #FFFFFF
Text:       #171717
Muted:      #737373
Accent:     #2563EB  (仅链接使用)
```

### 1.4 Serenity（宁静蓝绿）

```
Primary:    #0D9488  (teal-600)
Secondary:  #6366F1  (indigo-500)
Bg:         #F0FDFA
Surface:    #FFFFFF
Text:       #134E4A
```

### 1.5 流行渐变色组合

```css
/* 渐变 1: 日落金 */
background: linear-gradient(135deg, #f5af19, #f12711);

/* 渐变 2: 极光 */
background: linear-gradient(135deg, #667eea, #764ba2);

/* 渐变 3: 海洋 */
background: linear-gradient(135deg, #0ba360, #3cba92);

/* 渐变 4: 棉花糖 */
background: linear-gradient(135deg, #e0c3fc, #8ec5fc);

/* 渐变 5: 沙丘 */
background: linear-gradient(135deg, #d4a373, #e9c46a);
```

---

## 二、CSS 设计 Token 系统

### 2.1 SmartTourism 改造方案

```css
:root {
  /* === Brand === */
  --color-primary: #2D5A27;
  --color-primary-hover: #1A3A18;
  --color-primary-light: #E8F5E8;
  --color-primary-50: #f0f7ee;
  --color-primary-100: #d9eed5;
  --color-primary-500: #4caf50;
  --color-primary-700: #2d5a27;
  --color-primary-900: #1a3a18;

  /* === Secondary === */
  --color-accent: #C8944A;
  --color-accent-light: #F5E6CC;
  
  /* === Neutral scale === */
  --color-bg: #F5F0EB;
  --color-surface: #FFFFFF;
  --color-surface-hover: #F9F9F9;
  --color-border: #E8E8E8;
  --color-border-light: #F0F0F0;
  
  /* === Text === */
  --color-text-primary: #2C2C2C;
  --color-text-secondary: #666666;
  --color-text-muted: #999999;
  --color-text-inverse: #FFFFFF;

  /* === Semantic === */
  --color-success: #22C55E;
  --color-warning: #F59E0B;
  --color-error: #EF4444;
  --color-info: #3B82F6;

  /* === Spacing Scale (4px base) === */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-16: 64px;

  /* === Border Radius === */
  --radius-sm: 8px;      /* 按钮、输入框 */
  --radius-md: 12px;     /* 卡片 */
  --radius-lg: 16px;     /* 大卡片 */
  --radius-xl: 24px;     /* 模态框 */
  --radius-full: 9999px; /* 药丸形 */

  /* === Shadows === */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
  --shadow-md: 0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.04);
  --shadow-lg: 0 4px 16px rgba(0,0,0,0.08), 0 8px 32px rgba(0,0,0,0.04);
  --shadow-xl: 0 8px 32px rgba(0,0,0,0.12), 0 16px 64px rgba(0,0,0,0.06);

  /* === Typography === */
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-mono: "SF Mono", "Fira Code", "Consolas", monospace;

  /* === Transitions === */
  --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-normal: 250ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-slow: 350ms cubic-bezier(0.4, 0, 0.2, 1);
}
```

---

## 三、配色工具推荐

| 工具 | 用途 |
|------|------|
| [oklch.com](https://oklch.com) | OKLCH 色彩空间调色 |
| [realtimecolors.com](https://realtimecolors.com) | 实时配色预览 |
| [uicolors.app](https://uicolors.app) | Tailwind 色阶生成 |
| [huemint.com](https://huemint.com) | AI 配色生成 |
| [coolors.co](https://coolors.co) | 配色方案浏览 |

---

*SmartTourism 当前采用：暖土色系（Earthy Warm）*
