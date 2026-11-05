"""仅监听 127.0.0.1:8765 的本地服务。

手册入口：python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
项目入口：python -m app.main [--config ...] [--root-path ...]"""

from contextlib import asynccontextmanager
import argparse
from pathlib import Path
import secrets
import sys

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.auth import LocalSessionMiddleware, print_session_token
from app.config import Settings, load_settings, settings
from app.process_lock import data_directory_lock
from app.db import check_existing_database, DB_NAME, get_registered_root, read_database
from app.jobs import ScanManager, ScanConflict, ScanQueueFull, read_job, latest_job
from app.schemas import ScanRequest


HOST = '127.0.0.1'
PORT = 8765
ORIGIN = f'http://{HOST}:{PORT}'
VERSION = '0.1.0'
STATIC_DIR = Path(__file__).resolve().parent / 'static'
FRONTEND_DIST = STATIC_DIR / 'dist'


# 按给定配置创建 FastAPI 应用，登记生命周期、鉴权、页面和 API 路由。
def create_app(configuration: Settings = settings) -> FastAPI:
    # 1. 为本次应用创建独立的内存会话容器；定义生命周期时尚未取得锁或生成凭证。
    session = {}

    # 启动时取得进程锁并准备任务管理器与会话，停机时等待任务收尾并清理会话。
    @asynccontextmanager
    async def lifespan(app):
        # 先取得进程锁，再初始化会话和后台任务，避免同一数据目录被同时使用。
        with data_directory_lock(configuration.DATA_DIR):
            check_existing_database(configuration.DATA_DIR)
            manager = ScanManager(configuration) if (configuration.DATA_DIR / DB_NAME).is_file() else None
            app.state.scan_manager = manager
            # 生命周期启动：数据库与后台管理器准备好后才创建凭证，通过 yield 交出服务运行期。
            token = secrets.token_urlsafe(32)
            try:
                print_session_token(token, ORIGIN)
                session['token'] = token
                yield
            finally:
                # 生命周期结束：先等后台任务收尾，再清除凭证，最后离开外层并释放进程锁。
                if manager is not None:
                    manager.close()
                session.clear()

    # 2. 创建应用并登记鉴权中间件，使后面所有 API 统一经过本机会话检查。
    app = FastAPI(version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = configuration
    app.add_middleware(LocalSessionMiddleware, session=session, origin=ORIGIN)

    # 3. 登记公开健康接口与首页，静态资源只公开构建产物。
    # 公开返回固定健康状态和版本，不泄露配置、目录或凭证。
    @app.get('/health')
    async def health():

        return {'status': 'ok', 'version': VERSION}

    # 读取构建后的首页并填入固定模式标签，未构建时返回明确提示。
    @app.get('/', response_class=HTMLResponse)
    async def index():
        # 只填入固定模式标签，不把原始配置值或路径写进公开页面。
        entry = FRONTEND_DIST / 'index.html'
        if not entry.is_file():
            return HTMLResponse('前端尚未构建，请在项目根目录执行 npm run build。', status_code=503)
        page = entry.read_text(encoding='utf-8')
        return HTMLResponse(page.replace('{{WRITE_MODE}}',
                            '允许写入' if configuration.WRITE_ENABLED else '只读')
                            .replace('{{MODEL_MODE}}',
                            'Mock（模拟）' if configuration.MODEL_PROVIDER == 'mock'
                            else '真实模型（尚未接入）'))

    # 只公开构建产物，不直接提供 Vue 源码和 npm 依赖目录。
    app.mount('/static', StaticFiles(directory=FRONTEND_DIST, check_dir=False), name='static')

    # 4. 登记会话状态与根筛选逻辑，网页仅拿到当前授权的编号/别名。
    # 向已鉴权页面返回模式、限制与当前可选择的授权根别名。
    @app.get('/api/status')
    async def status():
        return {'authenticated': True, 'cloud_enabled': configuration.CLOUD_ENABLED,
                'model_provider': 'mock' if configuration.MODEL_PROVIDER == 'mock' else 'real',
                'write_enabled': configuration.WRITE_ENABLED, 'max_files': configuration.MAX_FILES,
                'roots': authorized_roots()}

    # 筛选数据库中与当前本机授权一致且启用的根，仅返回编号和别名。
    def authorized_roots():
        if configuration.ROOT_PATH is None or not (configuration.DATA_DIR / DB_NAME).is_file():
            return []
        with read_database(configuration.DATA_DIR) as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM roots WHERE enabled=1')]
        result = []
        for root_id in ids:
            try:
                get_registered_root(configuration, root_id)
            except (OSError, ValueError):
                continue
            result.append({'id': root_id, 'alias': root_id})
        return result

    # 把根授权校验失败转换为接口错误，阻止访问未知或未授权的目录。
    def require_root(root_id):
        try:
            get_registered_root(configuration, root_id)
        except PermissionError:
            raise HTTPException(403, detail='root_not_authorized') from None
        except (OSError, ValueError):
            raise HTTPException(404, detail='unknown_or_unavailable_root') from None

    # 5. 登记扫描 POST 与任务查询 GET；实际扫描交给管理器，GET 只读取已有状态和报告。
    # 受理带幂等编号的扫描申请，返回任务编号并转换冲突或容量错误。
    @app.post('/api/scans', status_code=202)
    def create_scan(request: ScanRequest, idempotency_key: str = Header(min_length=1, max_length=128)):
        require_root(request.root_id)
        manager = app.state.scan_manager
        if manager is None:
            raise HTTPException(409, detail='initialize_database_first')
        try:
            return manager.submit(request.root_id, request.verify_content, idempotency_key)
        except ScanConflict:
            raise HTTPException(409, detail='idempotency_conflict') from None
        except ScanQueueFull:
            raise HTTPException(429, detail='scan_queue_full') from None

    # 读取指定任务，重新校验所属根授权后返回状态和报告。
    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str):
        if app.state.scan_manager is None:
            raise HTTPException(404, detail='unknown_job')
        job = read_job(configuration.DATA_DIR, job_id)
        if job is None:
            raise HTTPException(404, detail='unknown_job')
        require_root(job['root_id'])
        return job

    # 校验根授权后返回该根最近一次任务，供页面恢复或刷新报告。
    @app.get('/api/scans/latest/{root_id}')
    def get_latest_scan(root_id: str):
        require_root(root_id)
        return {'job': latest_job(configuration.DATA_DIR, root_id)}

    return app


# 解析启动配置和阶段参数，并限制自动重载只能用于只读的 A 阶段。
def startup_options(argv):
    parser = argparse.ArgumentParser(description='FileMindDemo local server')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--root-path')
    parser.add_argument('--phase', choices=('A', 'B'), default='B',
                        help='A: development; B: execution tests and trials (default)')
    parser.add_argument('--reload', action='store_true', help='Auto-restart on Python edits (phase A only)')
    args = parser.parse_args(argv)
    if args.reload and args.phase != 'A':
        parser.error('--reload 仅允许在 --phase A 开发阶段使用')
    config_args = []
    if args.config is not None:
        config_args.extend(['--config', str(args.config)])
    if args.root_path is not None:
        config_args.extend(['--root-path', args.root_path])
    configuration = load_settings(config_args)
    if args.reload and configuration.WRITE_ENABLED:
        parser.error('--reload 要求 WRITE_ENABLED=false；写入执行测试请使用阶段 B')
    return args, configuration


# 供重载工作进程重新读取原启动参数，创建使用相同授权配置的应用。
def create_app_from_cli():

    _, configuration = startup_options(sys.argv[1:])
    return create_app(configuration)


# 读取启动参数并启动仅监听本机回环地址的 Uvicorn 服务。
def main():
    args, configuration = startup_options(sys.argv[1:])
    options = dict(host=HOST, port=PORT, proxy_headers=False, access_log=False, workers=1)
    if args.reload:
        uvicorn.run('app.main:create_app_from_cli', factory=True, reload=True,
                    reload_dirs=[str(STATIC_DIR.parent)], **options)
    else:
        uvicorn.run(create_app(configuration), reload=False, **options)


# 为 Uvicorn 的模块入口创建应用对象；这里只登记结构，锁和会话凭证在实际启动时初始化。
app = create_app()


if __name__ == '__main__':
    main()
