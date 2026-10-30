# Desktop Agent

基于大模型的桌面端智能体项目骨架。应用使用 Python 后端与 HTML/JS 前端，通过 PyInstaller 打包为桌面应用。

> 当前状态：已实现四种基础工具与可运行的 ReAct 终端循环；桌面 UI、FastAPI/WebSocket 尚未接入。

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

## 终端对话测试

启动前先在 `.env` 中配置模型：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=your-model-name
```

在项目根目录运行：

```bash
python -m agent.core --workspace .
```

进入后可直接在 `You >` 提示符输入问题。输入 `/exit` 或 `/quit` 退出。默认每个用户请求最多执行 6 个模型轮次，可用 `--max-steps` 修改。

当前工具：

- `read(path, offset, limit)`：分页、带行号读取文本
- `write(path, content)`：自动创建目录并原子写入
- `edit(path, old, new)`：只允许精确且唯一的字符串替换
- `bash(command, timeout)`：带硬超时执行命令

JSON Schema 位于 `agent/tools.py` 的 `TOOL_SCHEMAS`，OpenAI tools 包装位于 `OPENAI_TOOLS`。
## 后端服务

从项目根目录启动：

```bash
uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
```

也可以使用：

```bash
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

### WebSocket 事件流

连接地址：

```text
ws://127.0.0.1:8000/ws/{session_id}
```

发送用户文本：

```json
{"type": "user_message", "content": "读取 README.md 并总结"}
```

服务端推送的主要事件：

- `thinking`：高层执行进度，不包含模型隐藏思维链
- `tool_call`：工具名称与参数，例如正在读取哪个文件
- `tool_result`：工具返回内容，命令输出位于 `result.stdout` / `result.stderr`
- `final`：最终回答
- `error`：服务端错误，包含 `level: "error"`，前端应标红显示
- `interrupted`：Agent 已被中断

### 中断 Agent

通过 WebSocket 发送：

```json
{"type": "interrupt"}
```

或调用 HTTP 接口：

```bash
curl -X POST http://127.0.0.1:8000/api/sessions/{session_id}/interrupt
```

## 打包

```bash
pyinstaller packaging/desktop-agent.spec
```

## 后续开发

- [ ] 实现 pywebview 桌面入口
- [x] 实现 FastAPI 与 WebSocket 服务
- [x] 实现 Agent 主循环
- [ ] 实现工具注册与权限确认
- [ ] 接入 MCP Client
