import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.process_lock import data_directory_lock


# 验证同数据目录排斥第二个进程，释放锁后允许再次取得锁。
def test_second_process_rejected_and_release_allows_restart(tmp_path):
    script = '''
from pathlib import Path
from app.process_lock import data_directory_lock
import sys
try:
    with data_directory_lock(Path(sys.argv[1])):
        print('acquired')
except RuntimeError:
    sys.exit(2)
'''
    # 启动另一个 Python 进程竞争同一数据目录锁，收集退出码和输出。
    def contender():
        return subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                              capture_output=True, text=True, timeout=10)
    with data_directory_lock(tmp_path):
        assert contender().returncode == 2
    assert contender().returncode == 0


# 验证持锁进程被强制终止后操作系统释放锁，其他进程可重新使用。
def test_crashed_process_releases_os_lock(tmp_path):
    script = '''
from pathlib import Path
from app.process_lock import data_directory_lock
import sys, time
with data_directory_lock(Path(sys.argv[1])):
    print('ready', flush=True)
    time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                               stdout=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == 'ready'
        with pytest.raises(RuntimeError):
            with data_directory_lock(tmp_path):
                pass
        process.kill()
        process.wait(timeout=10)
        with data_directory_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()


# 验证不同数据目录可同时持锁，指向同一目录的别名不能绕过锁。
def test_separate_directories_allowed_and_alias_rejected(tmp_path):
    first = tmp_path / 'first'
    alias = tmp_path / 'alias'
    first.mkdir()
    alias.symlink_to(first, target_is_directory=True)
    with data_directory_lock(first), data_directory_lock(tmp_path / 'second'):
        with pytest.raises(RuntimeError):
            with data_directory_lock(alias):
                pass


# 验证服务整个生命周期持有锁，停机后允许再次启动。
def test_app_holds_lock_until_shutdown(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr(main, 'print_session_token', lambda *args: printed.append(args))
    config = Settings(DATA_DIR=tmp_path)
    with TestClient(main.create_app(config), base_url=main.ORIGIN):
        with pytest.raises(RuntimeError, match='DATA_DIR'):
            with TestClient(main.create_app(config), base_url=main.ORIGIN):
                pass
        assert len(printed) == 1
    with TestClient(main.create_app(config), base_url=main.ORIGIN):
        assert len(printed) == 2


# 验证启动期间异常也会释放进程锁，不阻止后续启动。
def test_failed_startup_releases_lock(monkeypatch, tmp_path):
    # 模拟终端初始化异常，用于检查启动失败后进程锁是否释放。
    def fail(*args):
        raise RuntimeError('terminal unavailable')
    monkeypatch.setattr(main, 'print_session_token', fail)
    with pytest.raises(RuntimeError, match='terminal unavailable'):
        with TestClient(main.create_app(Settings(DATA_DIR=tmp_path))):
            pass
    with data_directory_lock(tmp_path):
        pass


# 验证未指定 A 阶段或处于 B 阶段时禁止自动重载。
@pytest.mark.parametrize('argv', [['--reload'], ['--phase', 'B', '--reload']])
def test_reload_rejected_outside_phase_a(argv):
    with pytest.raises(SystemExit) as error:
        main.startup_options(argv)
    assert error.value.code == 2


# 验证默认启动关闭重载并仅启用一个服务工作进程。
def test_default_launch_disables_reload(monkeypatch):
    calls = {}
    monkeypatch.setattr(sys, 'argv', ['app.main'])
    monkeypatch.setattr(main.uvicorn, 'run', lambda app, **kwargs: calls.update(kwargs))
    main.main()
    assert calls['reload'] is False
    assert calls['workers'] == 1


# 验证 A 阶段重载使用应用工厂，并保留本机配置和 CLI 根授权。
def test_phase_a_reload_preserves_cli_config(monkeypatch, tmp_path):
    config = tmp_path / 'local.toml'
    config.write_text('DATA_DIR = "custom-data"\n')
    root = tmp_path / 'documents'
    root.mkdir()
    monkeypatch.setattr(sys, 'argv', ['app.main', '--phase', 'A', '--reload',
                                    '--config', str(config), '--root-path', str(root)])
    captured = {}
    # 拦截服务器启动并记录重载选项，避免测试真正启动服务。
    def run(app, **kwargs):
        captured.update(kwargs)
        assert app == 'app.main:create_app_from_cli'
    monkeypatch.setattr(main.uvicorn, 'run', run)
    main.main()
    assert captured['reload'] is True and captured['factory'] is True
    app = main.create_app_from_cli()
    assert app.state.settings.DATA_DIR == tmp_path / 'custom-data'
    assert app.state.settings.ROOT_PATH == root


# 验证启用文件写入时即使处于 A 阶段也拒绝自动重载。
def test_reload_rejected_when_write_enabled(tmp_path):
    config = tmp_path / 'local.toml'
    config.write_text('WRITE_ENABLED = true\n')
    with pytest.raises(SystemExit):
        main.startup_options(['--phase', 'A', '--reload', '--config', str(config)])
