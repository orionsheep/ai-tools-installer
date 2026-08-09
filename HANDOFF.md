# 开发者交接文档（LPT 定制版）

> 本仓库 fork 自 [bandusix/easy-codex-and-claude-cli-setup](https://github.com/bandusix/easy-codex-and-claude-cli-setup)（MIT），
> 由 LPT 团队改造为「学员一键安装 + 内置模型配置」的离线安装器。
> 本文档面向接手的开发者，读完即可独立维护和迭代。

## 这是什么

一个 Windows/macOS 离线 GUI 安装器（PyInstaller 单文件 exe / dmg），双击即可：

1. 离线安装 3 个 AI Coding CLI：**Codex、Claude Code、Kimi Code**（已移除 Gemini CLI / Lark CLI）
2. 自动写入三个工具的模型配置（内置 Step Plan，阶跃星辰），学员装完即用
3. 支持手动配置窗口，学员/用户可填自己的 Base URL + API Key 覆盖内置配置

## 仓库结构（只需要关心两个文件）

```
gui_installer.py            # 全部安装 + 配置 + GUI 逻辑（单文件，~1000 行，Python/tkinter）
.github/workflows/build.yml # 构建配方：下载 payload → 注入 secret → PyInstaller → 发 Release
ci/windows_smoke.py         # 端到端冒烟测试脚本（真机 Windows 上跑真实安装 + 真实调模型）
.github/workflows/smoke-windows.yml # 每次打 tag 自动在干净 Windows 机器上跑冒烟测试
```

冒烟测试很关键：它在一台全新的 Windows runner 上调用 gui_installer.py 里
真实的 install_* / configure_* 函数，再分别跑 `codex exec`、`claude -p`、
`kimi -p` 真实请求模型。任何 Windows 特有的破坏（PATH shim、沙箱弹窗、
npm 残留）都会让 CI 变红。改完代码先看这个 workflow 绿了再发版。

其余文件（LICENSE / CHANGELOG / assets）均来自上游，基本不用动。

## 构建与发布流程

**不需要本地装任何构建环境**。整个构建在 GitHub Actions 完成：

```bash
git add -A && git commit -m "xxx"
git tag v1.0.0-buildN          # tag 以 v 开头即触发构建
git push origin main v1.0.0-buildN
```

约 3 分钟后，` Releases ` 页面自动出现新的 `AI_Tools_Installer_Windows_<tag>.exe` 和 mac dmg（文件名带版本号，如 `..._v1.0.0-build10.exe`）。

学员侧只发这一个链接即可（永远指向最新版）：

```
https://github.com/orionsheep/ai-tools-installer/releases/latest
```

## 内置密钥是怎么注入的（重要）

- Step 的 API key **不在 git 仓库里**，存在仓库的 GitHub Secret：`BUILTIN_LLM_CONFIG`
- 构建时 `build.yml` 的 "Inject builtin LLM config" 步骤把 secret 写成 `payload/builtin_config.json` 打进安装包
- 运行时 `gui_installer.py` 的 `_load_builtin_llm_config()` 读取它；本地开发没有这个文件时，内置配置选项自动失效（手动配置仍可用）

**换 key / 换模型**：改 secret 后重新打 tag 构建即可，不用改代码：

```bash
gh secret set BUILTIN_LLM_CONFIG -R orionsheep/ai-tools-installer --body '{"name":"Step Plan", ...}'
```

secret 的 JSON 结构（见 `configure_*` 函数的消费方式）：

```json
{
  "name": "Step Plan",
  "anthropic_env": { "ANTHROPIC_AUTH_TOKEN": "...", "ANTHROPIC_BASE_URL": "...", "ANTHROPIC_MODEL": "...", "...": "..." },
  "openai_base_url": "https://api.stepfun.com/step_plan/v1",
  "openai_api_key": "...",
  "model": "step-3.5-flash-2603",
  "codex_model": "step-3.7-flash"
}
```

## 三个工具的配置写法（都踩过坑，别乱改）

| 工具 | 协议 | 写入位置 | 注意 |
|---|---|---|---|
| Claude Code | Anthropic Messages | `~/.claude/settings.json` 的 `env` 块 + `~/.claude.json` 跳引导 | v2 的 npm 包是壳，真二进制在平台子包里——payload 直接内置 `@anthropic-ai/claude-code-win32-x64`，安装=纯解压，不装 Node |
| Codex | OpenAI **Responses** | `~/.codex/config.toml` + 用户环境变量 `RELAY_API_KEY` | Codex 已删除 `wire_api="chat"`（[openai/codex#7782](https://github.com/openai/codex/discussions/7782)），必须用 `responses`；Step 只有 **step-3.7-flash** 开了 Responses 接口，所以 Codex 单独用 `codex_model` |
| Kimi Code | Anthropic Messages | `~/.kimi-code/config.toml`（注册 anthropic 型 provider + default_model） | Kimi **只读配置文件**，不读 shell 环境变量 |

## 已知边界与坑

- **仅支持 x64 Windows**（payload 没有 ARM 包）；mac 支持 Apple Silicon + Intel
- 写 PATH 走注册表 + `WM_SETTINGCHANGE` 广播，**不要用 setx**（1024 字符截断 bug，会毁掉用户 PATH）
- npm 全局安装必须显式 `--prefix=<内置 node 目录>`，否则 shim 位置在 Windows/mac 上不一致
- exe 未签名，Windows SmartScreen 会拦截 → 用户点"更多信息 → 仍要运行"
- 所有学员共用内置 key，Step Plan 有"每 5 小时调用次数"限流；人多要改成一人一 key（换 secret + 构建即可，或做网页登录回调——尚未实现）

## 本地调试

```bash
python3 gui_installer.py   # 需要带 tkinter 的 Python（brew install python-tk）
```

配置文件写入逻辑可用假 HOME 安全自测（不污染本机）：

```bash
HOME=/tmp/fakehome python3 -c "import gui_installer as g; g.configure_claude_code({...})"
```

## 后续路线图（和产品方对齐过）

1. 一人一 key（new-api 批量生成）或网页登录回调（localhost callback 方案）
2. 充值/余额查询入口（跳网页，不在客户端做支付）
3. 内容位：模型/工具推荐推送
