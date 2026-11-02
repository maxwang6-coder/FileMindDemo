# FileMindDemo 项目规格

## 依据与阶段

业务依据：《个人文件检索找回与整理助手开发手册》第 5—7、22—30 章。仓库名采用 FileMindDemo。明确的用户变更与 [设计决策](decisions.md)共同记录对手册的调整，不依赖聊天记忆重定义协议。

当前实现 A0 本地安全入口，A1 扫描尚未开始。A0 不调用真实模型、不读取资料正文、不修改资料文件；启动会创建应用 DATA_DIR 与锁文件，这不等于修改用户资料。

## A0 当前契约

### 配置

`app/config.py` 定义不可变 Settings；本机 TOML 覆盖默认值，CLI `--root-path` 覆盖 TOML 中的授权根。默认值：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| DATA_DIR | 仓库下 `data` | 应用运行数据，不是待扫描资料 |
| ROOT_PATH | 未授权 | 未设置时拒绝授权路径访问 |
| CLOUD_ENABLED | false | 不允许云调用 |
| MODEL_PROVIDER | mock | 测试用模拟模式 |
| WRITE_ENABLED | false | 不启用资料写入 |
| MAX_FILES | 500 | 后续扫描的文件数上限配置 |

默认读取仓库根的 `config.local.toml`（存在时）；支持 CLI 指定配置。TOML 相对路径以配置所在目录为基准，CLI 相对路径以当前工作目录为基准。不从 HTTP 或环境变量授权 ROOT_PATH。当前目录校验与 `authorized_path()` 不能代替 A1 每层不跟随链接的扫描防护。

### 会话与 HTTP

- 精确同源地址：`http://127.0.0.1:8765`。项目 CLI 固定监听回环地址，直接使用 Uvicorn 时显式填写相同 host/port。
- 服务启动生命周期中取得 DATA_DIR 进程锁，生成随机 token，仅打印到本机交互终端；无可用本机终端时启动失败。停机清除会话引用。
- `/api` 及 `/api/` 下所有路径与方法均要求 `Authorization: Bearer ...`，包括未知 API 路由。不接受 URL token。
- 所有 HTTP 请求校验 Host；Origin 存在且非空时精确匹配。缺少或空 Origin 不自动拒绝，但仍检查 Host 和 API token。重复安全头按现有规则拒绝。
- 未授权 API 返回 401；不允许的 Host 返回 400；不允许的 Origin 返回 403。通过中间件后，路由仍可返回自己的 404/405 等状态。
- 不开放 CORS，WebSocket 暂不支持。响应禁止缓存，脚本仅同源；Element Plus 布局需要允许内联样式，不允许内联脚本或 unsafe-eval。

| 当前路由 | 返回内容 | 边界 |
| --- | --- | --- |
| GET /health | `{"status":"ok","version":"0.1.0"}` | 无需 token，不含根目录或密钥 |
| GET / | Vue 构建页面与固定模式标签 | 未构建时 503；非 Mock 标注“真实模型（尚未接入）” |
| GET /api/status | authenticated、cloud_enabled、model_provider、write_enabled、max_files | token 必须有效；不序列化完整 Settings |
| /static | 构建产物 | 不公开 Vue 源码、配置或 npm 依赖 |

网页 token 仅在组件内存中保存；通过请求头发送，不放 URL、cookie、localStorage、sessionStorage 或 console。断开与页面离开清理会话。当前 GET 路由不修改用户文件、不打开本机程序。

### 启动与锁

支持 `python -m app.main` 与手册的 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8765`。导入模块登记应用结构；取得锁、生成 token 发生于服务生命周期。

同一 DATA_DIR 只允许一个服务持有操作系统锁；崩溃后操作系统释放锁，残留空锁文件不代表仍有服务。不要删除运行中的锁文件。

CLI 默认 phase B、单 worker、无 reload；仅 phase A 且 WRITE_ENABLED=false 时可使用 reload。这是启动策略，不是开发进度。后续应用内变更锁负责串行协调扫描与执行，当前尚无扫描/执行线程可协调。

项目 CLI 关闭 Uvicorn 访问日志和代理头信任；直接调用 Uvicorn 使用其自身启动选项，不能假定两种入口的服务器选项完全相同。

## A0 验证与当前限制

现有安全测试检查未授权 401、错误 token/URL token、Host/Origin、公开页面字段与模式、静态文件；进程测试检查同目录排斥、独立目录与锁释放。检查以实际测试输出为准，本规格不记录未经本轮执行的通过数量。

验证命令见 [README](../README.md)。网页测试依赖已构建前端；只构建前端不会启动服务。

## 后续阶段约束

- A1：初始化数据库、自写样例与 manifest、扫描身份协调；先列状态表和临时目录测试。DATA_DIR 在资料根外；根及祖先链接、每层符号链接、排除目录、边界文件必须验证。不得 chmod 或修改扫描源文件。
- A2—A4：受限解析子进程、可追溯分块、稀疏检索、增量版本失效；源文件打开使用 POST、ID 与后端校验。
- A5—A7：真实 embedding/模型 Provider、受限只读工具、独立评测与试用。Mock 不计入真实模型效果，公开结果不包含私人资料。
- B0—B1：建议、虚拟标签、预览与版本绑定确认，仍不移动文件。
- B2—B5：仅测试副本起步；同盘普通文件、不覆盖、持久 journal、恢复核对与条件撤销。未通过故障测试不得开放写功能。

数据库采用手册第 23 章的版本、状态及外键约定，接口与错误码以第 24—25 章为依据，随对应阶段实现；这些不是当前已存在的表或路由。不要一次性实现整个后续规格。

## 数据与评测准备

样例资料与源码、DATA_DIR 分开。A1 实现 `scripts.seed_demo` 后再生成手册第 26 章的自写文档，已存在目标拒绝覆盖；边界夹具使用临时目录。当前脚本为空，不能据此宣称样例已生成。

可先记录 20 个真实找文件问题及人工基线，答案先于结果确定，私人内容本机保存。正式评测按第 29 章建立 60 条查询，按文档家族切分 36 开发/24 测试；不使用测试答案调参。当前 eval 文件为空，不是已完成评测集。
