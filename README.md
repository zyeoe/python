# Desktop Agent

基于大模型的桌面端智能体项目骨架。应用使用 Python 后端与 HTML/JS 前端，通过 PyInstaller 打包为桌面应用。

> 当前状态：已实现 pywebview 桌面入口、FastAPI/WebSocket 后端、三栏桌面 UI、ReAct 循环与四种基础工具；`read` 支持文本分页和常见图片，并提供稳定 mock、渐进披露技能系统、MCP 工具、AGENTS.md 注入、代码级 hooks 与隔离 reviewer。

## 架构说明

- 桌面窗口：`pywebview`
- 后端：FastAPI 内部 sidecar
- 实时通信：WebSocket
- 智能体循环与工具：`agent/`
- 技能系统：`skills/`，启动时只加载名称和 description
- MCP 工具：`mcp_servers/` 与 `mcp.json`
- 行为文件：`AGENTS.md` 与便携全局层 `AGENTS.global.md`
- 硬约束：`agent/hooks.py`
- 子 agent：`agent/reviewer.py`
- 前端界面：`ui/`

FastAPI 仅绑定 `127.0.0.1`，由桌面应用内部启动；产品形态是桌面窗口，而不是 localhost 网页。

## 目录结构

```text
python/
├─ main.py
├─ agent/
│  ├─ __init__.py
│  ├─ core.py
│  ├─ loop.py
│  ├─ customization.py
│  ├─ hooks.py
│  ├─ mcp_client.py
│  ├─ mock_client.py
│  ├─ reviewer.py
│  ├─ selftest.py
│  ├─ skills.py
│  └─ tools.py
├─ app/
│  ├─ __init__.py
│  ├─ inspect.py
│  └─ selftest.py
├─ skills/
│  ├─ run-tests/SKILL.md
│  ├─ review-diff/SKILL.md
│  └─ fetch-issue/SKILL.md
├─ mcp_servers/
│  └─ issue_server.py
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
├─ tests/
├─ AGENTS.md
├─ AGENTS.global.md
├─ skill_report.json
├─ custom_report.json
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

复制 `.env.example` 为 `.env`，然后填写模型服务的 API Key。未配置远端模型时，`AGENT_MODEL_BACKEND=auto` 会自动使用稳定 mock，因此界面和工具链仍可离线测试。

## 配置

- `.env`：运行环境变量
- `AGENT_MODEL_BACKEND`：`auto`、`openai` 或 `mock`
- `ENABLE_SKILLS`：`1` 启用技能系统，`0` 回退到 v1.0 行为
- `SKILLS_DIR`：技能目录，默认 `./skills`
- `ENABLE_MCP`：`1` 启用 MCP 工具，`0` 禁用
- `ENABLE_AGENTS`：`1` 注入 AGENTS.md，`0` 禁用
- `ENABLE_HOOKS`：`1` 启用代码级 hook，`0` 回退到无 hook 行为
- `ENABLE_REVIEWER`：`1` 暴露隔离 reviewer，`0` 禁用
- `AGENTS_GLOBAL_PATH`：可选的全局 AGENTS.md 路径
- `mcp.json`：MCP Server 列表
- `packaging/desktop-agent.spec`：PyInstaller 打包配置

## 终端对话测试

启动前先在 `.env` 中配置模型：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=your-model-name
AGENT_MODEL_BACKEND=auto
```

在项目根目录运行：

```bash
python -m agent.core --workspace .
```

进入后可直接在 `You >` 提示符输入问题。输入 `/exit` 或 `/quit` 退出。默认每个用户请求最多执行 6 个模型轮次，可用 `--max-steps` 修改。

当前工具：

- `read(path, offset, limit)`：分页、带行号读取文本；PNG/JPEG/GIF/WebP 会作为多模态图片交给模型
- `write(path, content)`：自动创建目录并原子写入
- `edit(path, old, new)`：只允许精确且唯一的字符串替换
- `bash(command, timeout)`：带硬超时执行命令

JSON Schema 位于 `agent/tools.py` 的 `TOOL_SCHEMAS`，OpenAI tools 包装位于 `OPENAI_TOOLS`。

### 离线 mock 模式

没有配置 API Key 时，`AGENT_MODEL_BACKEND=auto` 会自动回退到稳定 mock，不需要网络即可启动：

```bash
python -m agent.core --workspace .
```

也可以显式指定：

```powershell
$env:AGENT_MODEL_BACKEND = "mock"
python -m agent.core --workspace .
Remove-Item Env:AGENT_MODEL_BACKEND
```

## 基线自检

运行不联网的基线自检：

```powershell
python -m agent.selftest --out selftest_report.json
```

自检覆盖 `write` 自动建目录、文本分页、图片读取、`edit` 唯一替换、`bash` 执行、图片进入模型、渐进披露、`use_skill`、技能开关、MCP 工具、UI 工具事件和打包配置。命令成功时退出码为 0，并输出机器可读 JSON。

## 技能系统

三个技能分别覆盖三种形态：

- `run-tests`：纯提示词流程
- `review-diff`：提示词 + `scripts/collect_diff.py`
- `fetch-issue`：提示词 + MCP 工具 `mcp__issues__fetch_issue`

启动时 system prompt 只包含技能名称和 description。模型判断需要后调用 `use_skill(name)`，此时才加载完整正文。关闭技能系统：

```powershell
$env:ENABLE_SKILLS = "0"
python -m agent.core --workspace .
Remove-Item Env:ENABLE_SKILLS
```

生成技能验收 JSON：

```powershell
python -m app.inspect skills --out skill_report.json
```

报告由程序扫描技能目录、计算 token、执行 8 条触发试验并统计误触发，不使用手写数字。

### MCP 配置

默认 `mcp.json` 启动本地 stdio MCP 服务：

```json
{
  "mcpServers": {
    "issues": {
      "command": "$PYTHON",
      "args": ["-m", "mcp_servers.issue_server"],
      "cwd": ".",
      "env": {}
    }
  }
}
```

`fetch-issue` 技能会调用该服务暴露的 `fetch_issue`。MCP 服务不可用时，技能会明确报告错误，不会伪造 issue 内容。

## 定制层

### AGENTS.md

启动时按以下顺序拼进 system prompt：

1. 用户目录 `~/AGENTS.md`
2. 项目最近一层 `AGENTS.md`

如果用户目录没有全局文件，则使用仓库自带的便携层 `AGENTS.global.md`。项目文件始终覆盖在全局文件之后。

### Hooks

- `PreToolUse`：拒绝写 `.env`、`*.pem`、`*.key`，理由固定为“密钥不入库，用环境变量”。
- `PreToolUse`：拒绝 `rm -rf`、force push、`git reset --hard`，并返回安全替代方案。
- `PostToolUse`：`write` / `edit` 后自动跑测试，失败时只回注最多 3 行摘要。

关闭 hooks：

```powershell
$env:ENABLE_HOOKS = "0"
python -m agent.core --workspace .
Remove-Item Env:ENABLE_HOOKS
```

### Reviewer

主 agent 可通过 `review_changes` 工具启动 reviewer。reviewer 使用独立消息上下文，只读取 git diff，不暴露写工具，只把最终审查结果返回主上下文。

生成定制层验收 JSON：

```powershell
python -m app.inspect custom --out custom_report.json
```

报告里的拒绝记录、红绿测试和 reviewer token 数据均由程序实际执行生成。

源码运行时 `$PYTHON` 自动使用当前解释器。打包后的程序如果无法自动找到 Python，可在 `.env` 中指定：

```dotenv
MCP_PYTHON=C:\Path\To\python.exe
```

## 前端界面

`ui/` 使用原生 HTML/CSS/JS 实现三栏桌面界面：

- 左侧资源管理器：读取 `/api/workspace/tree`
- 中间代码阅读区：读取 `/api/workspace/file`，带行号和轻量语法高亮
- 右侧对话与工具调用面板：通过 WebSocket 实时渲染 `thinking`、`tool_call`、`tool_result`、`final`、`error`、`interrupted`

默认连接 `http://127.0.0.1:8000`。pywebview 启动时可以注入：

```js
window.AGENT_API_BASE = "http://127.0.0.1:8000";
```

也可以使用查询参数：

```text
ui/index.html?apiBase=http://127.0.0.1:8000&file=agent/core.py
```

工具结果中的命令输出、文件 diff 和错误会显示在右侧工具调用面板；错误使用红色样式。
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

打包前先创建 `.env`，或者打包完成后把 `.env` 放在 exe 同目录：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 和模型配置
```

安装依赖后，在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
```

### 单文件 exe

```powershell
$env:BUILD_MODE = "onefile"
python -m PyInstaller --clean --noconfirm packaging\desktop-agent.spec
Remove-Item Env:BUILD_MODE
```

输出：`dist\desktop-agent.exe`。

### 文件夹版本

```powershell
$env:BUILD_MODE = "onedir"
python -m PyInstaller --clean --noconfirm packaging\desktop-agent.spec
Remove-Item Env:BUILD_MODE
```

输出：`dist\desktop-agent\desktop-agent.exe`。

打包配置已经设置 `console=False`，双击 exe 会直接显示 pywebview 窗口，不会弹出控制台黑框。应用会在 `127.0.0.1` 上自动选择空闲端口启动 FastAPI，因此不会与常见的 8000 端口冲突。

配置读取顺序：

1. 操作系统环境变量
2. exe 同目录的 `.env`
3. 打包进程序的 `.env`

单文件版本可以把 `.env` 一起打进 exe；如果需要后续修改 API Key，建议把 `.env` 放在 exe 同目录，它会覆盖内置配置。注意单文件 exe 中的密钥可以被解包读取，不适合作为高安全性的密钥存储方案。

如果启动失败，程序会在 exe 同目录写入 `desktop-agent-error.log` 并弹出错误提示。
## 后续开发

- [x] 实现 pywebview 桌面入口
- [x] 实现 FastAPI 与 WebSocket 服务
- [x] 实现 Agent 主循环
- [x] 实现渐进披露技能系统
- [x] 接入 MCP Client
- [x] 实现 AGENTS.md 自动注入
- [x] 实现代码级 hooks
- [x] 实现隔离 reviewer 子 agent
- [ ] 实现工具注册与权限确认
