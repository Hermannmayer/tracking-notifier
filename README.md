# 📦 快递追踪通知系统

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

支持 **流通王(ScoreJP)** 和 **OCS** 的物流追踪，定时检查状态变化并自动推送通知。

## 功能

- **多快递商**：流通王(ScoreJP) / OCS，支持扩展
- **浏览器自动化**：通过 Chrome DevTools Protocol (CDP) 操作真实浏览器查询物流
- **自动归档**：已签收包裹通知一次后自动归档，不再追踪
- **自动续 Cookie**（需搭配 AI Agent 编排工具）：OCS Cookie 过期时自动打开登录页 → 视觉识别验证码 → 重新登录
- **状态分类**：待揽收 → 运输中 → 清关中 → 派送中 → 已签收 / 异常
- **变化推送**：仅当状态发生变化时输出通知，静默运行无噪音

## 快速开始

### 依赖

```bash
pip install -r requirements.txt
```

需要一个运行中的 Chrome/Chromium 实例（或 CloakBrowser），开启 CDP 调试端口（默认 `localhost:9222`）。

### 配置

复制环境变量模板并填入你的凭据：

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OCS_USERNAME` | OCS 官网登录账号（手机号） | — |
| `OCS_PASSWORD` | OCS 官网登录密码 | — |
| `CDP_URL` | Chrome DevTools Protocol 地址 | `http://localhost:9222` |
| `TRACKER_DATA_DIR` | 数据目录（DB + Cookie） | `./data/` |
| `CAPTCHA_PATH` | 验证码截图保存路径 | `/tmp/ocs_captcha.png` |

> 如果只用流通王(ScoreJP)，不需要 OCS 账号。

### 用法

```bash
# 添加追踪
python3 tracker.py add "OCS:12345678901" "备注信息"
python3 tracker.py add "流通王:9876543210" "备注信息"

# 查看所有追踪中的包裹
python3 tracker.py list

# 手动检查所有包裹
python3 tracker.py check

# 快速查单个
python3 tracker.py check-one "OCS:12345678901"

# 移除追踪
python3 tracker.py remove <包裹ID>

# 手动保存 OCS Cookie
python3 tracker.py cookie OCS <ASP.NET_SessionId>
```

## 自动化（定时检查 + 自动续 Cookie）

配合 [Hermes Agent](https://hermes-agent.nousresearch.com) 或其他 AI Agent 编排工具使用：

```yaml
# Hermes cron 配置
schedule: "0 1,5,6 * * 1-6"    # 周一到六 09:00/13:00/14:00 BJT
runtime: agent                   # Agent 驱动模式
toolsets: [terminal, file, vision, browser]
```

每次检查时会：
1. 运行 wrapper → 检查所有包裹
2. 有状态变化 → 自动推送通知
3. OCS Cookie 过期 → agent 打开浏览器 → 视觉识别验证码 → 自动续 Cookie → 重新检查

### cron wrapper

```bash
python3 tracker-cron-wrapper.py
```

wrapper 会根据输出自动区分：状态更新 / Cookie过期 / 系统错误 / 无变化，不同场景不同处理。

## 项目结构

```
tracking-notifier/
├── tracker.py                 # 主程序（查询 + 状态管理）
├── tracker-cron-wrapper.py    # cron 包装器（智能检测输出类型）
├── requirements.txt           # Python 依赖
├── .env.example               # 环境变量模板
├── data/                      # 运行时数据目录
│   ├── tracker.db             # SQLite 数据库
│   └── ocs_cookies.json       # OCS session cookie 持久化
└── README.md
```

## 支持的物流商

| 物流商 | 查询方式 | 是否需要登录 |
|--------|----------|-------------|
| **流通王(ScoreJP)** | 官网公开查询（无验证码） | 否 |
| **OCS** | 官网登录后查询 | 是（需 OCS 账号 + 验证码） |

## 技术栈

- Python 3.8+
- Chrome DevTools Protocol (CDP) via WebSocket
- SQLite3（本地状态持久化）
- BeautifulSoup4（HTML 表格解析）

## 协议

Apache License 2.0

详见 [LICENSE](LICENSE) 文件。
