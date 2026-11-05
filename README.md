# FileMindDemo

本机运行的个人文件检索与整理助手，依据《个人文件检索找回与整理助手开发手册》逐阶段开发。

当前实现 A0（本地安全入口）和 A1 五步（数据库、自写样例、目录边界、稳定哈希、身份协调、范围故障处理、扫描报告与页面）。解析、搜索、定时扫描、真实模型、整理与恢复尚未实现。浏览器人工点击验收和用户独立练习仍待记录。

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

DATA_DIR 保存程序数据，ROOT_PATH 指向允许读取的资料；两者必须分开。本机配置与数据库根登记已拒绝 DATA_DIR 位于资料根内、根路径或祖先包含符号链接。目录遍历基础跳过链接、排除目录与跨设备子目录；文件级扫描检查尚待实现。

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

## 修改代码后的 Git 保存与上传

以下操作在仓库根目录执行，适用于已经初始化 Git 并连接 GitHub 远程仓库的项目。先在编辑器中保存文件（macOS 为 `Command+S`）；Git 记录的是磁盘上的内容。Git 命令不要求激活 Python 虚拟环境。

### 1. 查看本次修改

```sh
git status
git --no-pager diff
```

`git status` 显示已修改、未跟踪和已暂存的文件；`git diff` 查看已跟踪文件尚未暂存的具体修改。新文件的内容需直接打开检查，或暂存后检查。`--no-pager` 将内容直接输出，不用逐页按 Enter；普通 `git diff` 进入翻页界面后可按 `q` 退出。

提交前按修改范围验证：前端改动先构建，代码行为改动运行相关测试；需要完整检查时使用上文的构建与 pytest 命令。仅修改文档时检查内容、链接和命令即可。

### 2. 选择这次要保存的文件（暂存）

例如，本次只准备提交 README 的修改：

```sh
git add README.md
```

提交其他文件时，将 `README.md` 换成实际文件路径，也可在一条命令中列出多个路径。`git add` 把当时的文件内容放入“暂存区”，相当于准备本次提交的清单；之后再次修改该文件，需要重新 `git add` 才会纳入后续修改。

确认所有改动都属于本次提交时，也可以执行 `git add .`，它会暂存当前目录下未被忽略的新增、修改和删除。不要未经检查就全部暂存：`.gitignore` 不会阻止已跟踪文件中的敏感内容被提交。

### 3. 检查真正准备提交的内容

```sh
git status
git diff --cached --stat
git --no-pager diff --cached
```

`--cached` 查看暂存区与上一次提交之间的差异，也就是这次 commit 准备保存的修改。只看某个文件可执行：

```sh
git --no-pager diff --cached -- README.md
```

确认没有真实资料、个人绝对路径、token、API key、本机配置、数据库或运行日志。二进制文件的 diff 通常不展示内容，需要单独检查文件。若误暂存 README，可执行下面命令取消暂存，文件修改仍保留：

```sh
git restore --staged -- README.md
```

### 4. 保存到本地仓库（commit）

```sh
git commit -m "docs: 补充 Git 保存与上传教程"
git status
```

提交说明应换成这次实际做的事。`git commit` 只保存暂存区中的内容，形成一条本地历史记录，不会上传 GitHub；没有暂存的修改会继续留在工作目录。以后再次修改、add、commit，会在已有历史后追加新的提交，之前未 push 的提交也会保留。

### 5. 上传到 GitHub（push，可稍后执行）

如果当前分支已经设置远程跟踪关系：

```sh
git push
```

若提示当前分支没有 upstream，先用 `git remote -v` 确认 `origin` 指向自己的目标仓库，再执行：

```sh
git push -u origin HEAD
```

这会把当前分支推送到 `origin` 的同名分支，并设置跟踪关系，后续直接 `git push`。如果提示远程有本地没有的提交，先检查差异并合并或变基，不要直接使用强制推送。

上传后可以检查：

```sh
git status
git log -5 --oneline
```

`working tree clean` 只说明没有未提交的文件改动；本地仍可能有尚未上传的提交。设置跟踪关系后，`ahead ...` 表示比本地记录的远程分支多出提交。`up to date` 依据最近一次获取的远程信息，并不表示从未修改过代码。

日常顺序是：**保存文件 → 检查改动和验证 → add → 检查暂存内容 → commit → 按需 push**。commit 是保存本地版本历史，push 才是上传；不用为了关机而每次都 push。

## 文档与开发范围

- [开发守则](AGENTS.md)：AI 协作与安全约束。
- [项目规格](docs/spec.md)：当前行为、阶段范围及验收。
- [设计决策](docs/decisions.md)：实现选择及与手册的差异。
- [数据库字段说明](app/db.py)：四张现有表的建表 SQL 均有中文字段注释，包含状态全部允许值及含义。SQLite 无独立列 COMMENT，这些注释不会自动显示在已有数据库查看器的字段备注栏。
- [本机会话说明](docs/local-session.md)：启动、token 与进程锁细节。
- [学习记录](docs/learning.md)与[失败案例](docs/badcases.md)：持续积累理解和可复现问题。

## A1 第一步：数据库与样例

在项目根目录执行（本机已执行，不需要重复生成样例）：

```sh
.venv/bin/python -m scripts.init_db
.venv/bin/python -m scripts.seed_demo --dest ./demo_docs
```

初始化可重复执行；样例生成遇到已有目标、边界目录或本机配置就拒绝覆盖。生成 9 个手册样例和 4 个文件名边界样例、`fixture_manifest.json`，将样例目录登记为 root_id `demo`。这是样例登记，不是扫描。

数据库为 `data/filemind.sqlite3`；本机授权配置为 `data/config.local.demo.toml`，含个人路径，已被忽略。若要用这套样例配置启动：

```sh
.venv/bin/python -m app.main --config data/config.local.demo.toml
```

这仍不会开始扫描。存在数据库时服务启动会检查 schema version，不支持的版本拒绝启动，不自动重置数据库。

详见 [A1 实现步骤：第一步](<docs/A1 Steps.md#step-1>)。

## A1 第二步：只读清点文件元数据

```sh
.venv/bin/python -m scripts.scan --config data/config.local.demo.toml --root-id demo --metadata-only
```

输出 JSON，`mode=metadata_only`，列出相对路径、字节数、纳秒时间、设备号与 inode。当前只接纳普通 TXT/MD/PDF/DOCX 文件（扩展名不区分大小写），跳过隐藏项、排除目录、链接和其他类型。

必须在本机配置或 CLI 明确授权 ROOT_PATH，且它与数据库中 root_id 对应的路径、设备号一致，记录启用；数据库中的旧记录本身不能扩大本次授权。不带样例配置时，默认 ROOT_PATH 为空会被拒绝，也可用 `--root-path ./demo_docs` 明确授权。

该命令不读正文、不算哈希、不分配或改变 file_id、不更新数据库、不改 manifest，不打开网页或启动后台任务。MAX_FILES 超限或遍历失败时返回失败（`complete=false`、非零退出码），不返回一个冒充完整结果的截断列表。服务运行时先正常停止，再运行 CLI；不要删除锁文件。

详见 [A1 实现步骤：第二步](<docs/A1 Steps.md#step-2>)。

## A1 第三步：分块哈希与读取稳定性

```sh
.venv/bin/python -m scripts.scan --config data/config.local.demo.toml --root-id demo --verify-content
```

本节的 `--verify-content` 和下文正式扫描都会读取源文件字节：按 1MiB 更新同一个 SHA256 状态，比较读取前后设备号、inode、大小、mtime_ns、ctime_ns，并复核路径当前仍指向同一文件。发现变化丢弃该次哈希，等待 1 秒重试一次；仍变化返回 unstable，sha256=null。

结果模式为 content_hashes。stable 表示本次读取通过检查，不等于正文 ready；failed、unstable、too_large 均不提供可用哈希。只要有一个候选未稳定读取，complete=false，命令返回非零退出码，其他稳定结果仍保留供诊断。

默认每文件上限 20MiB，超限明确返回 too_large，不截断后计算哈希。需要调整时本机命令可增加 `--max-file-mib 30`；输入必须为正整数。文件读取权限不变，不解析正文、不上传资料、不修改文件或数据库、不改变 UUID。读取后的未来修改仍需后续版本校验。

详见 [A1 实现步骤：第三步](<docs/A1 Steps.md#step-3>)。该模式不写数据库。

## A1 第四步：身份协调与同步入库

```sh
.venv/bin/python -m scripts.scan --config data/config.local.demo.toml --root-id demo --sync
```

`--sync` 自动进行稳定哈希检查，并更新应用 SQLite 的 files 表；第五步已支持按范围隔离故障。新文件分配 UUID；同路径保留编号，内容变化增加 content_rev；旧路径消失后，唯一设备号/inode/哈希匹配且无硬链接歧义时保留编号并增加 path_rev。相同内容副本有不同编号。确认消失的旧记录标 missing，保留历史，不删除源文件。

`--metadata-only` 与 `--verify-content` 两种检查不更新文件记录。同步不会重写 manifest，也不会修改源文件；WRITE_ENABLED=false 不妨碍更新应用自己的登记簿。运行中的服务持有同一 DATA_DIR 锁时，先停止服务再运行此命令。

第四步的身份规则见 [A1 实现步骤：第四步](<docs/A1 Steps.md#step-4>)；故障隔离和默认命令已由第五步完善，见下文。启动后端不会自动扫描。

## A1 第五步：范围故障、报告与页面

```sh
# 正式扫描，默认等同于 --sync；必须由本机配置授权目录
.venv/bin/python -m scripts.scan --config data/config.local.demo.toml --root-id demo

# 使用页面扫描：先启动服务，再打开 http://127.0.0.1:8765 并输入终端 token
.venv/bin/python -m app.main --config data/config.local.demo.toml --phase A --reload
```

页面只可选已授权 root_id；点击“扫描目录”启动单后台 worker，显示候选/稳定/新增/修改/改名/缺失/不可用/失败计数、跳过与错误原因。一个目录不可读不影响其他目录；失败范围的旧记录 unavailable，只有成功完整枚举并复核消失才 missing。文件超限/读取失败/不稳定单独隐藏；MAX_FILES 总量超限或提交前状态变化整批不应用。

每次正式扫描保存 scan_id、UTC 起止时间、数量、错误码与报告，刷新页面可查看最近报告；服务重启不自动重放未完成任务。数据库迁移到 schema 2，保留 roots/files，新增 jobs/idempotency。旧 schema 1 会在服务启动、init_db 或正式扫描 CLI 持进程锁时迁移；未知版本仍拒绝，不重置数据。

正式扫描 JSON 的 mode=root_scan；部分成功 complete=false、applied=true，CLI 非零退出，已成功范围的结果仍保存。完全中止 applied=false。正式命令、页面与文件身份集成测试统一调用 scan_root；第四步旧同步入口及报告类型已删除。详见 [A1 实现步骤：第五步与验收](<docs/A1 Steps.md#step-5>)。

公开仓库仅放源码、自写样例与脱敏结果。真实资料、密钥、数据库、缓存和运行日志保留本机；Git 提交前检查暂存差异。文件操作日志不等于备份，未来 B 阶段也必须先使用测试副本。
