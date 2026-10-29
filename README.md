# Desktop Agent

基于大模型的桌面端智能体项目骨架。应用使用 Python 后端与 HTML/JS 前端，通过 PyInstaller 打包为桌面应用。

> 当前状态：仅包含目录结构与基础配置，尚未实现业务代码。

## 架构说明

- 桌面窗口：`pywebview`
- 后端：FastAPI 内部 sidecar
- 实时通信：WebSocket
- 智能体循环与工具：`agent/`
- 前端界面：`ui/`
- 外部工具扩展：MCP，通过 `mcp.json` 配置

FastAPI 仅绑定 `127.0.0.1`，由桌面应用内部启动；产品形态是桌面窗口，而不是 localhost 网页。

## 目录结构

```text
python/
├─ main.py
├─ agent/
│  ├─ __init__.py
│  ├─ core.py
│  ├─ loop.py
│  └─ tools.py
├─ server/
│  ├─ __init__.py
│  ├─ app.py
│  ├─ main.py
│  └─ websocket.py
├─ ui/
│  ├─ index.html
│  ├─ app.js
│  └─ style.css
├─ packaging/
│  └─ desktop-agent.spec
├─ mcp.json
├─ requirements.txt
├─ .env.example
└─ README.md
```

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，然后填写模型服务的 API Key。

## 配置

- `.env`：运行环境变量
- `mcp.json`：MCP Server 列表
- `packaging/desktop-agent.spec`：PyInstaller 打包配置

## 打包

```bash
pyinstaller packaging/desktop-agent.spec
```

## 后续开发

- [ ] 实现 pywebview 桌面入口
- [ ] 实现 FastAPI 与 WebSocket 服务
- [ ] 实现 Agent 主循环
- [ ] 实现工具注册与权限确认
- [ ] 接入 MCP Client
