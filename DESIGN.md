# Dog Cloud Monitor Design

## Direction

明亮的个人网络仪表盘，像一张维护良好的运行日报：白色主画布、轻微冷灰分区、青绿色作为唯一主要交互色，琥珀色仅用于需关注状态。

## Color

- Background: `oklch(1 0 0)`
- Surface: `oklch(0.975 0.006 188)`
- Surface strong: `oklch(0.945 0.012 188)`
- Ink: `oklch(0.22 0.025 218)`
- Muted: `oklch(0.48 0.025 218)`
- Primary: `oklch(0.56 0.11 188)`
- Primary pale: `oklch(0.93 0.045 188)`
- Success: `oklch(0.56 0.13 154)`
- Warning: `oklch(0.68 0.14 72)`
- Danger: `oklch(0.58 0.19 28)`

## Typography

使用 Geist 与系统中文无衬线字体。界面标签 13–14px，正文 14–16px，关键数字 24–34px；数值使用等宽数字。产品界面不使用展示字体。

## Layout

桌面最大宽度 1440px。顶部为紧凑状态栏；首屏依次为总览数字、流量趋势、链接列表与最近 IP。移动端改为单列，表格转为可横向滚动的紧凑列表。

## Components

- 状态点配合文字说明，不单独依赖颜色。
- 容器圆角 12–14px；使用边框或轻阴影二选一。
- 主按钮和选中状态使用青绿色，其余控件保持中性。
- 流量条以同一尺度表现上下行构成，支持精确数字。
- 数据说明紧邻相关数值，避免隐藏在帮助页。

## Motion

仅在刷新、筛选和状态变化时使用 150–220ms 过渡；遵循 `prefers-reduced-motion`。
