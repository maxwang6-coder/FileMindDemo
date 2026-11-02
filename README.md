# FileMindDemo

本机运行的个人文件检索与整理助手，依据《个人文件检索找回与整理助手开发手册》逐阶段开发。

当前只实现 A0：配置、会话鉴权、同源 Vue 页面、健康接口和单进程锁。扫描、解析、搜索、真实模型、文件整理与恢复尚未实现；空模块和样例评测文件是后续阶段的占位。

## 环境与首次安装

当前开发环境使用 Python 3.13、Vue 3、Element Plus、Vite。其他 Python 版本尚未在本文中声明为已验证。Node.js 需满足 `package.json` 的 `>=22.12.0`；本机辅助工具使用 Node 24。

在仓库根目录的本机交互终端执行：

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
sh scripts/frontend.sh ci
sh scripts/frontend.sh run build
```

已有可用的 `.venv` 和 `node_modules` 时无需每次重装。辅助脚本优先使用系统 Node/npm，缺少时尝试本机 `.tools` 中的工具；新电脑需要自行安装 Node，脚本不会自动下载它。Python 与前端依赖分别锁定于 `requirements.lock.txt` 和 `package-lock.json`。

## 启动与停止

```sh
source .venv/bin/activate
python -m app.main
```

打开 <http://127.0.0.1:8765>，将本机终端显示的 Session token 粘贴到网页。每次服务重启生成新 token，刷新页面也需要重新输入。用启动服务的终端按 `Ctrl+C` 停止。

手册验收的另一种启动方式：

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

两种方式一次只运行一种，使用相同的应用生命周期、鉴权和数据目录锁。需要 `--config`、`--root-path` 或开发重载时使用项目 CLI：

```sh
python -m app.main --phase A --reload
```

CLI 默认 `--phase B` 表示关闭重载的启动限制，不表示项目进入 B 阶段。开发重载会重启服务并更换 token。B 执行测试与试用不得启用 reload；不要直接为 Uvicorn 添加多 worker 或 reload 参数。

## 配置与数据范围

没有本机配置时，默认 DATA_DIR 为仓库的 `data`，ROOT_PATH 未授权、云关闭、Mock、只读、最多 500 文件。A0 不会扫描资料；配置上限不代表扫描已经实现。

需要本机配置时，将 [配置模板](config.local.example.toml)复制为根目录的 `config.local.toml`，填写本机值。授权 ROOT_PATH 也可使用项目 CLI 的 `--root-path`。本机配置不提交 Git；不要在公开说明、截图或日志中填写真实个人路径。

DATA_DIR 保存程序数据，ROOT_PATH 指向允许读取的资料；两者必须分开。A1 将实现并验证数据目录在资料根之外、符号链接排除等扫描规则。目前配置模块尚不提供完整扫描级检查。

当前不读取 `.env` 授权目录；真实模型和密钥加载尚未接入，无需为 A0 填写 API key。

## 页面与接口

FastAPI 提供 Vite 构建后的 HTML、JavaScript 和 CSS，浏览器执行 Vue 页面，再请求同源 API，因此不需要另开前端服务器。

| 请求 | 鉴权 | 当前返回 |
| --- | --- | --- |
| `GET /` | 无需 token，仍校验 Host/Origin | 页面及只读、模型模式标签 |
| `GET /health` | 无需 token，仍校验 Host/Origin | `status`、`version` |
| `GET /api/status` | Bearer token | 会话状态及允许公开的模式字段 |

首页和 API 不返回真实资料根或模型密钥。所有 `/api` 路由要求 token。使用精确地址 `127.0.0.1:8765`，不要替换为 `localhost` 或其他端口。

## 修改前端与验证

修改前端后执行一次：

```sh
sh scripts/frontend.sh run build
```

或者另开终端执行 `sh scripts/frontend.sh run build:watch` 自动构建，之后刷新网页；现有后端终端无需重新启动。详情见 [前端说明](docs/frontend.md)。

运行现有测试前先构建页面：

```sh
sh scripts/frontend.sh run build
.venv/bin/python -m pytest -q
```

现有测试覆盖鉴权、Host/Origin、公开页面和进程锁；测试使用临时数据目录，不代表后续检索或整理功能已验收。测试结果必须以实际运行输出为准。

## 文档与开发范围

- [开发守则](AGENTS.md)：AI 协作与安全约束。
- [项目规格](docs/spec.md)：当前行为、阶段范围及验收。
- [设计决策](docs/decisions.md)：实现选择及与手册的差异。
- [本机会话说明](docs/local-session.md)：启动、token 与进程锁细节。
- [学习记录](docs/learning.md)与[失败案例](docs/badcases.md)：持续积累理解和可复现问题。

后续按 A1—A7 完成只读检索，再决定是否进入 B0—B5。尚未实现的 `python -m scripts.xxx` 命令不能当作可用工具；本项目当前不提供有效的扫描、评测或恢复命令。

公开仓库仅放源码、自写样例与脱敏结果。真实资料、密钥、数据库、缓存和运行日志保留本机；Git 提交前检查暂存差异。文件操作日志不等于备份，未来 B 阶段也必须先使用测试副本。
