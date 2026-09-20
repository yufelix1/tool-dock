# ToolDock

一个基于 Flask + Docker 的个人工作台，用于快速整合和管理常用工具。

---

## 当前工具

- **文件整理工具** → `/tools/file-flatten`
- **游戏录屏审阅** → `/tools/game-recording-review`
- **Torrent 转 Magnet** → `/tools/torrent-to-magnet`

录屏审阅工具支持配置多个游戏根目录，识别各目录下的
`游戏ID/目录/文件名.mp4`，并自动匹配同名的 `.jpeg`、`.jpg`、`.png`
或 `.webp` 封面。同一游戏 ID 会跨根目录汇总，可按游戏 ID 筛选。
跳过扫描目录需填写位于任一游戏根目录内的绝对路径。
录屏支持收藏、文本评论和批量删除，搜索可同时匹配目录、文件名与评论内容。

录屏审阅设置保存在容器内的 `/config/game-recording-review.json`，不依赖浏览器存储。
部署时请将持久化目录映射到 `/config`。如需修改容器内配置目录，可设置
`GAME_RECORDING_REVIEW_CONFIG_DIR` 环境变量。

---

## 快速启动

```bash
cd tool-dock
docker compose up --build -d
```

### 如何快速新增工具（推荐方式）

在 tools/ 目录下创建一个新文件夹，例如 tools/newtool/

在该文件夹内创建 routes.py（复制现有工具模板修改）

在 templates/ 下创建对应 HTML 页面（建议 templates/newtool/index.html）

修改 app.py，添加 Blueprint 注册：

Pythonfrom tools.newtool.routes import newtool_bp

app.register_blueprint(newtool_bp, url_prefix='/tools/newtool')

结构清晰、易扩展，欢迎持续扩充！
