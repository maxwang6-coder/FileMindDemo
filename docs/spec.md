# FileMindDemo 项目规格

## 依据与阶段

业务依据：《个人文件检索找回与整理助手开发手册》第 5—7、22—30 章。仓库名采用 FileMindDemo。明确的用户变更与 [设计决策](decisions.md)共同记录对手册的调整，不依赖聊天记忆重定义协议。

当前实现 A0 与 A1 五步：数据库、自写样例、目录边界、元数据、稳定分块 SHA256、身份协调、范围级故障处理、持久扫描报告与 UI。工程检查通过不代表用户已完成独立练习或浏览器人工验收。正式 CLI 默认扫描入库，--sync 是别名；--metadata-only 只看元数据，--verify-content 只检查稳定哈希。均不解析正文、不调用模型、不修改原资料。启动不自动扫描；A2 解析与 A4 定时扫描仍未实现。

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

默认读取仓库根的 `config.local.toml`（存在时）；支持 CLI 指定配置。TOML 相对路径以配置所在目录为基准，CLI 相对路径以当前工作目录为基准。不从 HTTP 或环境变量授权 ROOT_PATH。已拒绝根及祖先路径中的符号链接、DATA_DIR 在 ROOT_PATH 内；范围控制由根登记校验与扫描的逐层不跟随链接检查共同完成，不依赖未接入的预留方法。

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
| GET /api/status | 会话、模式、限制与当前授权根 ID/别名 | token 必须有效；不序列化 Settings 或绝对路径 |
| POST /api/scans | root_id、verify_content；202 job_id | Bearer 与 Idempotency-Key；拒绝额外路径字段，单任务容量，满时 429 |
| GET /api/jobs/{id} | 状态、阶段、起止时间、报告与错误码 | 重新校验任务 root 当前授权；未知 ID 404，无扫描副作用 |
| GET /api/scans/latest/{root_id} | 最近任务或 null | 仅已授权根，只读历史 |
| /static | 构建产物 | 不公开 Vue 源码、配置或 npm 依赖 |

网页 token 仅在组件内存中保存；通过请求头发送，不放 URL、cookie、localStorage、sessionStorage 或 console。断开与页面离开清理会话。当前 GET 路由不修改用户文件、不打开本机程序。

### 启动与锁

支持 `python -m app.main` 与手册的 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8765`。导入模块登记应用结构；取得锁、生成 token 发生于服务生命周期。

同一 DATA_DIR 只允许一个服务持有操作系统锁；崩溃后操作系统释放锁，残留空锁文件不代表仍有服务。不要删除运行中的锁文件。

CLI 默认 phase B、单 worker、无 reload；仅 phase A 且 WRITE_ENABLED=false 时可使用 reload。这是启动策略，不是开发进度。服务持有进程锁，ScanManager 用单后台 worker 和应用变更锁串行扫描；后续执行须共享该锁。CLI 独立取得进程锁，不与服务同时运行。

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

数据库采用手册第 23 章的版本、状态及外键约定；当前 roots/files/jobs/idempotency 四张表，PRAGMA user_version=2。显式迁移 1→2 保留旧根与文件；未知版本拒绝，未初始化的 A0 仍可启动。初始化/正式 CLI/服务启动持进程锁时迁移。其他表随阶段实现，不要一次性实现整个后续规格。

## 数据与评测准备

样例资料与源码文件、DATA_DIR 分开。已实现 `scripts.init_db` 与 `scripts.seed_demo`，详见 [A1 实现步骤：第一步](<A1 Steps.md#step-1>)。样例根登记 root_id、规范化绝对路径、设备号；根不可重叠，ID 不可重新绑定。manifest 使用 UUID、相对路径和逻辑别名，不写用户绝对路径。

生成器只登记自己创建的 13 个文件，状态为 discovered，sha256 为 NULL。它不是对任意资料目录的扫描，D08/D09 本轮也不会被判为 unsupported/failed。已有目标、根外边界目录或本机样例配置一律拒绝覆盖；意外失败保留现场、数据库事务回滚，不自动删目录重试。

目录遍历基础只枚举允许的目录，不跟随链接，跳过隐藏目录、常见系统/云目录、应用包及跨设备子目录。早期严格检查函数仍向上传递遍历错误；正式 scan_root 按目录隔离故障，不能转换成 missing。该边界不抵御同账号恶意并发路径替换，也不能识别所有第三方同步软件的自定义目录。

第二步新增 `collect_file_metadata` 及 CLI，见 [A1 实现步骤：第二步](<A1 Steps.md#step-2>)。仅处理普通文件，允许 .txt/.md/.pdf/.docx（不区分大小写），用 lstat 收集 relpath、size、mtime_ns、ctime_ns、st_dev、st_ino。名称相同的不同相对路径分别返回；不推断文件身份，不读正文，不校验文件格式，不更新数据库或 manifest。

查询根记录使用只读 SQLite 连接。当前 ROOT_PATH 必须非空、通过路径校验，与启用的 roots 记录路径和设备号一致；未知、禁用、路径不匹配或设备号变化均拒绝。只读清点不新增根授权。

MAX_FILES 是允许的候选文件数上限；超限立即失败，不截断后宣称完整。单个 lstat 或目录遍历异常仍使严格元数据检查失败；正式扫描按第五步规则隔离故障，不把不可见旧项标 missing。

第三步增加 `collect_file_hashes`，命令加 --verify-content 才读取字节，结果为 content_hashes，仍不写数据库。每次 read 请求 1MiB，逐块更新同一个 SHA256。比较枚举/打开、读取前后和路径复核时的 dev/ino/size/mtime_ns/ctime_ns，且已读字节数必须等于读取前 size；首次变化丢弃哈希、等待 1 秒重试一次，第二次仍变化为 unstable。

只读打开使用目录描述符、每层 O_NOFOLLOW、最终 O_NONBLOCK，并在读取前 fstat 验证普通文件及设备号；读完安全重开路径验证它仍指向同一对象。该实现针对当前 Mac/Unix，不宣称防御同账号恶意并发篡改，也不能阻止检查后再次修改。

文件结果状态 stable/unstable/failed/too_large 属于本次哈希报告，不是直接写入 files.status。仅 stable 有 sha256；其他状态为 null。哈希阶段单文件失败保留其他稳定结果，但 complete=false，CLI 非零退出；元数据枚举阶段的失败仍中止整次检查。未稳定读取的文件不得作为索引输入，当前尚未实现索引写入。

每文件默认 20MiB，本机 --max-file-mib 可调整为其他正整数；超过上限或读取中增长超限均不给出截断哈希。哈希不代表格式有效，不证明引用内容或当前可用版本，更不能作为 file_id。

第四步新增显式 --sync，自动哈希并协调 files；原 metadata_only/content_hashes 仍不写库。实现与验证见 [A1 第四步](<A1 Steps.md#step-4>)。新文件 UUID，优先匹配同一 root 下非 missing 的相对路径；已知哈希变化增加 content_rev、置 stale。旧哈希为 NULL 时只补初始基线，不推测历史内容变化。同路径不因 inode 改变而换号，路径优先支持编辑器替换保存。

仅在旧路径已确认消失、dev/ino/sha256 三者一致、旧候选和当前候选均唯一且当前 st_nlink=1 时推断同盘改名，保留 UUID、first_seen 与 content_rev，path_rev 增一。其他新路径新 UUID，旧记录 missing；同内容副本、改名同时改内容、硬链接歧义均不合并。历史 missing 不因同名再出现自动复活。

身份协调和正式扫描统一通过 scan_root 使用下文第五步策略；第四步旧同步入口、专用报告类型与未接入的 Settings 路径方法已移除。正式流程复核来源与授权、短事务比对旧快照、失败回滚；只写应用数据库，不改源文件与 manifest，无需启用 WRITE_ENABLED。身份及安全测试验证正式流程，不再维护旧版整批拒绝规则。

现有文件身份与范围状态规则：

| 情况 | 预期行为 |
| --- | --- |
| 新增 | 新 file_id |
| 同路径内容修改 | 保留身份，更新内容版本与指纹 |
| 合格的唯一同盘改名 | 保留 ID，更新路径版本 |
| 相同内容副本 | 新 ID，相同哈希 |
| 完整遍历后确认消失 | missing，不删磁盘文件 |
| 无权限或遍历失败 | unavailable，不批量标缺失 |
| 读取中持续变化 | unstable，暂不索引 |

第五步 scan_root 每个目录完整枚举成功后才保存其精确目录项集合；中途迭代失败丢弃该目录的缺失证据。未观察到的旧路径只有在成功父目录中确实缺少对应项，且安全路径复核仍缺失时才 missing；失败目录及其子树、stat 失败或不再符合策略的旧路径置 unavailable。正常兄弟范围继续协调。结果 files 只包含稳定成功项，不把隐藏旧记录返回为当前可用文件。

单文件失败分别记录 failed/unstable；too_large 在数据库映射 unsupported，报告保留具体原因。旧记录保留最后已知哈希用于恢复后的版本比较，它只是历史值，当前状态不允许作为可用内容；失败观察 sha256=null，新失败文件无哈希。恢复稳定后按原 UUID 和旧哈希协调，内容变化增加 content_rev。

正式扫描的文件更新与成功/部分成功报告在同一个事务提交；全局异常回滚文件更新并另记 failed 报告，未知候选总数为 null，不伪造为零。complete=false/applied=true 表示已应用成功范围和不可用状态；complete=false/applied=false 表示本批未应用。CLI 非完整结果非零退出，API 任务成功执行但不完整时 state=succeeded、error_code=partial_scan。

POST /api/scans 立即返回 202，单后台 worker，不阻塞 HTTP；幂等键同参数复用 job_id，异参数 409，新任务满额 429。verify_content 接受布尔值；A1 尚无轻扫路径，初次与手动正式扫描始终完整稳定哈希，报告 verify_content=true。报告存 jobs.result_json，时间 UTC；界面按本机时区显示，轮询只报告真实阶段，不伪造百分比。服务重启将 queued/running 标 interrupted，用户主动重试，不自动重放。页面只显示当前本机授权的根 ID/别名，没有任意路径输入，断开只清除页面状态、不取消已受理任务。

可先记录 20 个真实找文件问题及人工基线，答案先于结果确定，私人内容本机保存。正式评测按第 29 章建立 60 条查询，按文档家族切分 36 开发/24 测试；不使用测试答案调参。当前 eval 文件为空，不是已完成评测集。
