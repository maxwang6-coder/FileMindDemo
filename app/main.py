"""Local server on 127.0.0.1:8765.

Manual entry: python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
Project CLI: python -m app.main [--config ...] [--root-path ...]
"""

from contextlib import asynccontextmanager
import argparse
from pathlib import Path
import secrets
import sys

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.auth import LocalSessionMiddleware, print_session_token
from app.config import Settings, load_settings, settings
from app.process_lock import data_directory_lock


HOST = '127.0.0.1'
PORT = 8765
ORIGIN = f'http://{HOST}:{PORT}'
VERSION = '0.1.0'
STATIC_DIR = Path(__file__).resolve().parent / 'static'
FRONTEND_DIST = STATIC_DIR / 'dist'


def create_app(configuration: Settings = settings) -> FastAPI:
    session = {}

    @asynccontextmanager
    async def lifespan(app):
        # Acquire before initializing the session or any future executor jobs.
        with data_directory_lock(configuration.DATA_DIR):
            token = secrets.token_urlsafe(32)
            try:
                print_session_token(token, ORIGIN)
                session['token'] = token
                yield
            finally:
                session.clear()

    app = FastAPI(version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = configuration
    app.add_middleware(LocalSessionMiddleware, session=session, origin=ORIGIN)

    @app.get('/health')
    async def health():
        # Deliberate allowlist: never serialize settings, paths, or credentials.
        return {'status': 'ok', 'version': VERSION}

    @app.get('/', response_class=HTMLResponse)
    async def index():
        # Substitute only fixed display labels, never raw configuration values.
        entry = FRONTEND_DIST / 'index.html'
        if not entry.is_file():
            return HTMLResponse('前端尚未构建，请在项目根目录执行 npm run build。', status_code=503)
        page = entry.read_text(encoding='utf-8')
        return HTMLResponse(page.replace('{{WRITE_MODE}}',
                            '允许写入' if configuration.WRITE_ENABLED else '只读')
                            .replace('{{MODEL_MODE}}',
                            'Mock（模拟）' if configuration.MODEL_PROVIDER == 'mock'
                            else '真实模型（尚未接入）'))

    # Only generated assets are public; Vue source and npm dependencies stay private.
    app.mount('/static', StaticFiles(directory=FRONTEND_DIST, check_dir=False), name='static')

    @app.get('/api/status')
    async def status():
        return {'authenticated': True, 'cloud_enabled': configuration.CLOUD_ENABLED,
                'model_provider': 'mock' if configuration.MODEL_PROVIDER == 'mock' else 'real',
                'write_enabled': configuration.WRITE_ENABLED, 'max_files': configuration.MAX_FILES}

    return app


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


def create_app_from_cli():
    # Uvicorn's reloader imports this factory in each new worker. Read the
    # original local CLI again so --config/--root-path survive every restart.
    _, configuration = startup_options(sys.argv[1:])
    return create_app(configuration)


def main():
    args, configuration = startup_options(sys.argv[1:])
    options = dict(host=HOST, port=PORT, proxy_headers=False, access_log=False, workers=1)
    if args.reload:
        uvicorn.run('app.main:create_app_from_cli', factory=True, reload=True,
                    reload_dirs=[str(STATIC_DIR.parent)], **options)
    else:
        uvicorn.run(create_app(configuration), reload=False, **options)


# Uvicorn imports this symbol. Creation only registers routes/middleware;
# the lock and token are initialized later, during server startup.
app = create_app()


if __name__ == '__main__':
    main()
