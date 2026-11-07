# 项目地图

- `main.py`：桌面入口，加载 `.env`、定位打包资源、启动 FastAPI 与 pywebview。
- `agent/core.py`：ReAct 循环，组合工具、技能、MCP、hooks 与 reviewer。
- `agent/tools.py`：`read` / `write` / `edit` / `bash` 四个基础工具。
- `agent/skills.py`：技能目录扫描、渐进披露与 `use_skill`。
- `agent/mcp_client.py`：MCP stdio 客户端，按 `mcp.json` 加载外部工具。
- `agent/hooks.py`：PreToolUse / PostToolUse 代码级约束。
- `agent/reviewer.py`：隔离上下文的只读 reviewer 子 agent。
- `server/main.py`：FastAPI、WebSocket、会话中断与事件流。
- `ui/`：原生 HTML/CSS/JS 三栏桌面界面。
- `skills/`：可独立带走的技能目录。
- `mcp_servers/`：本项目附带的 stdio MCP 服务。
- `packaging/desktop-agent.spec`：PyInstaller 单文件 / 文件夹配置。

# 构建与测试

```powershell
python -m app.selftest
python -m app.inspect skills --out skill_report.json
python -m app.inspect custom --out custom_report.json
python -m PyInstaller --noconfirm packaging\desktop-agent.spec
```

- 测试优先用 `unittest`，不要默认安装新的测试框架。
- 打包后必须实际启动 exe，并检查 `/health` 与 WebSocket。
- 不把 `dist/`、`.build-deps/`、`.env` 或日志提交进 Git。

# 代码风格

- 保持 Python 3.12，不引入 Python 3.13+ 才有的语法。
- 错误必须带可执行的下一步建议，不要 `except: pass`。
- 所有文件路径先解析，再判断相对路径和越界。
- 工具返回给模型的内容必须可 JSON 序列化。
- 技能正文只在 `use_skill(name)` 后加载，不能塞进启动提示词。

# 测试说明

- `app.selftest` 必须在 20 秒左右完成且不依赖网络。
- 修改工具或 hook 后同时运行 `app.selftest` 和 `app.inspect custom`。
- 修改技能目录后运行 `app.inspect skills`，保持误触发为 0。
- 测试失败时只回注“用例 / 原因 / 下一步”，不要灌完整 traceback。

# 安全约束

- 禁止通过 `write` / `edit` 修改 `.env`、`*.pem`、`*.key`。
- 禁止 `rm -rf`、`git push --force`、`git reset --hard`；命中时给出替代方案。
- API Key 只放仓库外 `.env` 或系统环境变量。
- 不在工具输出、日志或报告里打印密钥。

# 坑

- 打包后资源必须从 `sys._MEIPASS` 读取，不能依赖当前工作目录。
- `uvicorn.run(..., reload=True)` 不能用于打包后的 exe。
- `AGENT_WORKSPACE` 的相对路径以 exe / 项目目录为基准。
- MCP 在源码中自动使用当前解释器；打包环境可能需要设置 `MCP_PYTHON`。
- 技能目录必须按名称稳定排序，否则会破坏 prompt 缓存。
