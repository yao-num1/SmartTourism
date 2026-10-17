# 04 - 字体排印与层级系统

## 一、2025-2026 字体趋势

### 可变字体 (Variable Fonts)
单个字体文件包含多轴变化（字重/宽度/倾斜），减少加载体积，提升性能。

### 系统字体栈（最优性能）
```css
--font-system: -apple-system, BlinkMacSystemFont, "SF Pro Display",
               "PingFang SC", "Microsoft YaHei", sans-serif;
```

### 中文字体推荐
| 平台 | 推荐 | 适用场景 |
|------|------|----------|
| macOS/iOS | PingFang SC | 正文+标题 |
| Windows | Microsoft YaHei | 正文 |
| Android | Noto Sans SC | 正文+标题 |
| Web Font | 阿里巴巴普惠体 | 品牌标题 |

## 二、字号层级（Type Scale 1.250）

| 层级 | 字号 | 行高 | 字重 | 用途 |
|------|------|------|------|------|
| Display | 48-64px | 1.1 | 700 | Hero 主标题 |
| H1 | 28-36px | 1.2 | 700 | 页面标题 |
| H2 | 22-26px | 1.3 | 600 | 区块标题 |
| H3 | 16-18px | 1.4 | 600 | 子标题 |
| Body | 14-15px | 1.6 | 400 | 正文 |
| Caption | 12-13px | 1.5 | 400 | 辅助文字 |
| Micro | 10-11px | 1.4 | 500 | 标签/徽章 |

## 三、移动端字体建议

- 正文最小 15px（iOS 不建议小于 16px）
- 输入框文字 16px（防止 iOS 自动缩放）
- 按钮文字 14-16px

## 四、渐变文字（2025 爆款）
```css
.gradient-text {
  background: linear-gradient(135deg, #2d5a27, #c8944a);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
```
