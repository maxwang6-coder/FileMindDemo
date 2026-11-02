# 本机会话启动与验证

在项目根目录的本机交互终端运行：

前端已迁移至 Vue。首次获取项目需先安装前端依赖并构建；本机已构建。
前端修改后执行 `sh scripts/frontend.sh run build`。详见 `docs/frontend.md`。

```sh
source .venv/bin/activate
python -m app.main
```

手册 A0 的验收启动命令同样支持（需先激活虚拟环境）：

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

两种方式均使用同一套会话鉴权、进程锁和页面。一次只运行其中一种。
直接使用 Uvicorn 时读取默认配置及本机 config.local.toml；需要 --config 或
--root-path 参数时，使用项目 CLI `python -m app.main`。

打开 `http://127.0.0.1:8765`，将终端显示的 Session token 粘贴到页面。
不要使用 `localhost` 地址：Host 和 Origin 按上述精确地址校验。
按 Ctrl+C 停止服务。每次启动生成新 token；刷新页面后需重新输入。

本机启动参数示例：

```sh
python -m app.main --root-path /absolute/path/to/authorized/folder
python -m app.main --config /absolute/path/to/config.local.toml
```

授权目录仅从启动配置读取；网页没有修改授权目录的接口。

## 单进程与开发重载

服务启动时创建 DATA_DIR（如果还不存在），并持有其中 `.executor.lock` 的操作系统锁。
同一 DATA_DIR 的第二份服务会立即拒绝启动；不同 DATA_DIR 的锁互不影响。
锁在整个服务生命周期内保持，正常停止或进程崩溃后由操作系统释放。
磁盘上保留空锁文件是正常现象，不代表程序仍在运行；不要手动删除运行中的锁文件。
后续执行器和后台任务必须在取得此锁之后启动，并在释放锁之前停止。

项目 CLI 默认采用阶段 B 的启动限制，关闭自动重载，只运行一个 worker。
该参数表示启动限制，不表示项目已开发到阶段 B：

```sh
.venv/bin/python -m app.main
# 等价于显式指定阶段 B：
.venv/bin/python -m app.main --phase B
```

仅阶段 A 开发时可启用自动重载：

```sh
.venv/bin/python -m app.main --phase A --reload
```

重载会监测 app 目录中的 Python 文件变更，重新启动服务并更换会话 token。
阶段 B（执行测试与试用）拒绝 --reload；启用写入时也拒绝 --reload。
这些参数限制由项目 CLI 实施。直接调用 Uvicorn 时，按上面的验收命令运行，
不要额外添加 --reload 或 --workers。开发自动重载使用项目 CLI 的 --phase A --reload。
请在切换启动方式前先 Ctrl+C 停止旧服务。已有旧版进程需要重启才会获得锁保护。

## 状态页面

`GET /health` 无需 token，仅返回 `status` 和版本 `version`，不返回磁盘路径。
首页无需登录即可显示文件操作模式和模型模式：默认是“只读”和“Mock（模拟）”。
真实模型尚未接入，选择非 Mock 配置时首页明确标记“真实模型（尚未接入）”。
前端仅接收明确列出的状态字段，不接收 API key 或完整配置。
现有 GET 路由仅提供网页、脚本和状态，不修改用户文件，不启动外部应用打开文件。

## 安全边界

- 项目 CLI 固定绑定 127.0.0.1:8765；直接调用 Uvicorn 时必须显式指定 --host 127.0.0.1 --port 8765。
- token 使用 secrets.token_urlsafe(32) 生成，只写入 /dev/tty；没有本机终端则启动失败。
- token 不通过接口、文件或应用日志公开，停机时清除服务器会话引用。
- 网页通过 Authorization: Bearer 请求头携带 token；仅保存在页面内存，清空输入框，不写 URL、cookie 或浏览器存储。
- 中间件覆盖 /api 和 /api/ 下所有方法和路由，包括后续新增路由。
- 所有 HTTP 请求均检查 Host；Origin 存在且非空时必须精确匹配。拒绝重复的安全头。
- 未启用 CORS 中间件，不返回开放式跨源许可。项目 CLI 禁用代理请求头信任和访问日志；直接调用 Uvicorn 时使用其自身启动选项，不能假定这些选项与 CLI 相同。
- 页面和 API 同源；禁用缓存，限制脚本来源和页面嵌入；WebSocket 暂不支持。
- Host 与 Origin 精确匹配 http://127.0.0.1:8765，不使用其他监听地址或端口。

运行自动化安全测试：

```sh
python -m pytest tests/security/test_auth.py -q
```
