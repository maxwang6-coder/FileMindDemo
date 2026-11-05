"""持久扫描报告与容量受限的单线程后台任务管理。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
from uuid import uuid4

from app.db import database_session, get_registered_root, read_database, utc_now


# 分配扫描编号并写入等待任务，可复用调用方的数据库事务。
def create_scan_job(data_dir, root_id, *, db=None):
    scan_id = str(uuid4())
    # 在给定数据库连接中登记这次等待扫描的根目录与创建时间。
    def insert(connection):
        connection.execute('INSERT INTO jobs(id,root_id,state,created_at) VALUES(?,?,?,?)',
                           (scan_id, root_id, 'queued', utc_now()))
    if db is not None:
        insert(db)
    else:
        with database_session(data_dir) as connection:
            insert(connection)
    return scan_id


# 把等待任务切换为运行中，记录开始时间和尝试次数。
def start_scan_job(data_dir, scan_id):
    with database_session(data_dir) as db:
        cursor = db.execute("UPDATE jobs SET state='running',attempts=attempts+1,started_at=? WHERE id=? AND state='queued'",
                            (utc_now(), scan_id))
        if cursor.rowcount != 1:
            raise RuntimeError('Scan job is not queued')


# 在文件更新所在事务中保存最终报告、结束时间和任务完成状态。
def save_scan_result(db, scan_id, report):
    row = db.execute('SELECT started_at FROM jobs WHERE id=?', (scan_id,)).fetchone()
    report['started_at'] = row['started_at']
    report['finished_at'] = utc_now()
    code = None if report['complete'] else 'partial_scan'
    db.execute("UPDATE jobs SET state='succeeded',finished_at=?,error_code=?,result_json=? WHERE id=?",
               (report['finished_at'], code, json.dumps(report, ensure_ascii=False), scan_id))


# 为未结束任务保存失败状态与错误回执，不覆盖已经完成的结果。
def fail_scan_job(data_dir, scan_id, code):
    with database_session(data_dir) as db:
        row = db.execute('SELECT root_id,started_at FROM jobs WHERE id=?', (scan_id,)).fetchone()
        if row is None:
            return
        finished = utc_now()
        result = {'mode': 'root_scan', 'scan_id': scan_id, 'root_id': row['root_id'],
                  'complete': False, 'applied': False, 'counts': {}, 'file_count': None,
                  'skipped': {}, 'errors': [{'relpath': '', 'error_code': code}],
                  'started_at': row['started_at'], 'finished_at': finished}
        db.execute("UPDATE jobs SET state='failed',error_code=?,finished_at=?,result_json=? WHERE id=? AND state IN ('queued','running')",
                   (code, finished, json.dumps(result), scan_id))


# 按编号读取任务，并将保存的报告 JSON 转为可返回前端的结构。
def read_job(data_dir, scan_id):
    with read_database(data_dir) as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (scan_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    raw_result = result.pop('result_json')
    result['result'] = json.loads(raw_result) if raw_result else None
    result['progress'] = 'finished' if result['state'] in ('succeeded', 'failed', 'interrupted') else result['state']
    return result


# 查找指定根最近创建的任务，并读取其状态和报告。
def latest_job(data_dir, root_id):
    with read_database(data_dir) as db:
        row = db.execute('SELECT id FROM jobs WHERE root_id=? ORDER BY created_at DESC LIMIT 1', (root_id,)).fetchone()
    return read_job(data_dir, row['id']) if row else None


class ScanConflict(Exception):
    pass


class ScanQueueFull(Exception):
    pass

# 负责安排扫描，把实际遍历文件和计算哈希和更新清单工作交给scan_root()
class ScanManager:
    # 准备单工作线程、接单锁和变更锁，并将重启遗留任务标为中断。
    def __init__(self, configuration):
        self.configuration = configuration
        self.change_lock = threading.Lock()
        self._submission_lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='filemind-scan')
        self._pending = set()
        with database_session(configuration.DATA_DIR) as db:
            # 遗留等待或运行任务统一标中断，不能把数据库旧状态当成仍有线程执行。
            db.execute("UPDATE jobs SET state='interrupted',error_code='service_restarted',finished_at=? WHERE state IN ('queued','running')",
                       (utc_now(),))

    # 校验授权与幂等申请，限制并发受理后创建任务并交给后台线程。
    def submit(self, root_id, verify_content, key):
        # 1. 校验本机根授权，将请求参数规范化后计算摘要，用于判断同一申请是否换了内容。
        get_registered_root(self.configuration, root_id)
        payload = json.dumps({'root_id': root_id, 'verify_content': verify_content}, sort_keys=True)
        request_hash = hashlib.sha256(payload.encode()).hexdigest()
        with self._submission_lock:
            # 2. 接单检查与登记必须串行，避免两个请求同时认为当前没有扫描任务。
            with database_session(self.configuration.DATA_DIR) as db:
                # 3. 优先查幂等回执：同键同参数直接返回原任务编号，同键异参数拒绝。
                previous = db.execute("SELECT request_hash,response_json FROM idempotency WHERE scope='scan' AND key=?", (key,)).fetchone()
                if previous:
                    if previous['request_hash'] != request_hash:
                        raise ScanConflict('Idempotency key reused with different input')
                    return json.loads(previous['response_json'])
                # 重复申请已优先返回原回执；只有真正的新申请才需要检查当前受理容量。
                if self._pending:
                    raise ScanQueueFull('A scan is already queued or running')
                # 4. 空闲时新建等待任务；任务登记与申请回执在同一个事务中保存。
                scan_id = create_scan_job(self.configuration.DATA_DIR, root_id, db=db)
                response = {'job_id': scan_id}
                db.execute('INSERT INTO idempotency(scope,key,request_hash,response_json) VALUES(?,?,?,?)',
                           ('scan', key, request_hash, json.dumps(response)))
            self._pending.add(scan_id)
            try:
                # 5. 占用受理容量后交给单后台线程，不等待扫描完成即可返回 202 所需的任务编号。
                self._worker.submit(self._run, scan_id, root_id)
            except RuntimeError:
                # 6. 若线程池无法接单，释放容量并保存失败，不能留下永远等待的任务。
                self._pending.discard(scan_id)
                fail_scan_job(self.configuration.DATA_DIR, scan_id, 'worker_unavailable')
                raise
            return response

    # 持应用变更锁执行扫描，记录异常并在结束时释放任务受理容量。
    def _run(self, scan_id, root_id):
        from app.scan import scan_root
        try:
            # 1. 进入实际扫描前取得共享变更锁，后续执行器也须使用同一个锁对象。
            with self.change_lock:
                # 服务生命周期已经持有数据目录进程锁；工作线程只需持应用变更锁。
                scan_root(self.configuration, root_id, scan_id=scan_id, acquire_process_lock=False)
        except Exception:
            # 2. 扫描异常时补记未结束任务失败；已有终态不会被重复覆盖。
            fail_scan_job(self.configuration.DATA_DIR, scan_id, 'scan_aborted')
        finally:
            # 3. 无论成功失败都在接单锁内释放容量，允许受理下一次新的扫描。
            with self._submission_lock:
                self._pending.discard(scan_id)

    # 等待已受理的后台扫描收尾，再关闭工作线程池。
    def close(self):

        self._worker.shutdown(wait=True)
