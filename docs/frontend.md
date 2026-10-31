# Vue 前端

前端使用 Vue 3、Element Plus 和 Vite。后端继续使用 FastAPI。
Element UI 主要用于 Vue 2，因此本项目安装 Vue 3 对应的 Element Plus。

## 文件位置

- `app/static/index.html`：Vue 入口页面，保留后端填写的模式标签。
- `app/static/src/App.vue`：页面组件、token 输入与连接逻辑。
- `app/static/src/main.js`：加载 Vue、组件样式并挂载页面。
- `app/static/src/style.css`：页面布局和颜色。
- `package.json` / `package-lock.json`：前端依赖和锁定版本。
- `vite.config.js`：把源码构建到 `app/static/dist/`。

浏览器只访问构建产物，不直接执行 `.vue` 文件。原来的 `session.js` 已由 Vue 组件替代。
构建产物、node_modules、.tools 不提交 Git；前端源码和 package-lock.json 提交 Git。

## 安装与构建

JavaScript 依赖用 npm 安装，放在 node_modules；Python 依赖仍位于 .venv。
本机没有系统 Node 时，辅助脚本可使用项目 .tools 内已下载的官方 Node 工具。
换电脑后请先安装 Node.js 22.12 以上的受支持版本（建议 Node 24 LTS）。

```sh
# 首次下载项目或更新锁文件后，按锁定版本安装依赖
sh scripts/frontend.sh ci
# 构建网页
sh scripts/frontend.sh run build
# 启动后端
.venv/bin/python -m app.main
```

如果系统已安装 Node.js/npm，也可以直接使用 `npm ci` 和 `npm run build`。
当前本机已完成安装与首次构建，无须每次重装依赖。

打开 http://127.0.0.1:8765，粘贴本机终端中的 token。
不要双击 index.html，也不需要另开 Vite 开发服务器或开放 CORS。
前后端始终使用相同的协议、主机和端口。

## 开发时更新页面

```sh
sh scripts/frontend.sh run build:watch
```

另一个终端运行后端。前端源文件保存后会自动重新构建，手动刷新网页查看。
刷新会清除页面会话，需重新输入 token。停止监测构建时按 Ctrl+C。
单次修改也可以运行 `sh scripts/frontend.sh run build`，不需要开启监测。

## 安全约束

- token 仅在组件内存中保存；通过 Authorization: Bearer 传递。
- 不写入 URL、cookie、localStorage、sessionStorage 或 console。
- 断开、页面离开和组件卸载时清除会话，取消尚未完成的认证请求。
- 前端只接收模式标签，不接收模型密钥或授权目录。
- 浏览器使用本地打包的脚本与样式，不依赖外部 CDN。
- 为 Element Plus 运行时布局允许内联样式；脚本策略仍只有 self，不允许内联脚本或 unsafe-eval。

运行 Python 安全测试前先构建前端：

```sh
sh scripts/frontend.sh run build
.venv/bin/python -m pytest -q
```
