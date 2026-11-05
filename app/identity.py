"""A1 文件身份判断：只生成计划，不执行文件系统读取或 SQL 写入。"""

from collections import Counter
from dataclasses import dataclass
import re
from uuid import uuid4

from app.db import utc_now


@dataclass(frozen=True, slots=True)
class IdentityChange:
    file_id: str
    relpath: str
    action: str
    content_rev: int
    path_rev: int
    status: str
    sha256: str
    old_relpath: str | None = None


@dataclass(frozen=True, slots=True)
class MissingFile:
    file_id: str
    relpath: str
    status: str = 'missing'


@dataclass(frozen=True, slots=True)
class IdentityPlan:
    records: list[dict]
    changes: list[IdentityChange]
    missing: list[MissingFile]
    counts: dict[str, int]


# 按路径优先和唯一设备号、inode、哈希匹配规则生成文件身份更新计划。
# 只接收稳定合法哈希，不读文件或写库；证据不足分配新 UUID，历史缺失身份不按同名复活。
def plan_identity_changes(root_id: str, existing: list[dict], observations: list[dict], *,
                          confirmed_missing: set[str], single_link_paths: set[str]) -> IdentityPlan:
    # 1. 检查输入：旧记录必须属于当前根，新观察不能路径重复且必须带稳定合法哈希。
    if any(row['root_id'] != root_id for row in existing):
        raise ValueError('Identity records belong to a different root')
    paths = [item['relpath'] for item in observations]
    if len(paths) != len(set(paths)):
        raise ValueError('Duplicate observed relative paths')
    for item in observations:
        if item['status'] != 'stable' or not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256') or ''):
            raise ValueError('Only stable observations with SHA256 can be reconciled')
    # 2. 建立有效旧路径索引；已 missing 的历史行不能仅凭同名复活。
    active = [row for row in existing if row['status'] != 'missing']
    by_path = {row['relpath']: row for row in active}
    if len(by_path) != len(active):
        raise ValueError('Ambiguous active database paths')
    # 提取旧记录的设备号、inode 和内容哈希，作为改名匹配证据。
    def old_key(row):
        return row['dev'], row['ino'], row['sha256']
    # 提取本次观察的设备号、inode 和内容哈希，与旧记录进行匹配。
    def new_key(item):
        return item['st_dev'], item['st_ino'], item['sha256']
    # 3. 统计新旧匹配证据出现次数，只让旧路径已消失且证据唯一的记录参与改名匹配。
    old_counts = Counter(old_key(row) for row in active if row['sha256'] is not None)
    new_counts = Counter(new_key(item) for item in observations)
    rename_sources = {old_key(row): row for row in active
                      if row['sha256'] is not None and row['relpath'] in confirmed_missing
                      and old_counts[old_key(row)] == 1}
    # 4. 准备已分配编号、已认领旧身份集合，以及更新计划、报告动作与计数。
    allocated = {row['id'] for row in existing}
    used = set()
    records, changes = [], []
    counts = dict.fromkeys(('added', 'initialized', 'modified', 'renamed',
                            'metadata_updated', 'unchanged', 'missing'), 0)
    # 5. 逐个处理本次稳定文件，先尝试同路径旧记录；按路径排序保证处理顺序稳定。
    for item in sorted(observations, key=lambda value: value['relpath']):
        previous = by_path.get(item['relpath'])
        renamed = False
        # 5.1 无同路径记录时，才以唯一设备号/inode/哈希匹配改名；硬链接歧义会阻止继承编号。
        if previous is None and item['relpath'] in single_link_paths and new_counts[new_key(item)] == 1:
            candidate = rename_sources.get(new_key(item))
            if candidate is not None and candidate['id'] not in used:
                previous = candidate
                renamed = True
        # 5.2 未找到可继承身份则新建 UUID；找到则复制旧行，避免直接改动调用方的旧快照。
        if previous is None:
            file_id = str(uuid4())
            if file_id in allocated:
                raise ValueError('UUID collision; aborting instead of reusing an identity')
            allocated.add(file_id)
            row = {'id': file_id, 'root_id': root_id, 'content_rev': 1, 'path_rev': 1,
                   'status': 'discovered', 'first_seen': utc_now()}
            action = 'added'
        else:
            used.add(previous['id'])
            row = dict(previous)
            # 5.3 已知内容改变则增加内容版本并置 stale；仅改名增加路径版本，旧哈希为空只补基线。
            content_changed = previous['sha256'] is not None and previous['sha256'] != item['sha256']
            if content_changed:
                row['content_rev'] += 1
                row['status'] = 'stale'
                action = 'modified'
            elif renamed:
                row['path_rev'] += 1
                action = 'renamed'
            elif previous['sha256'] is None:
                action = 'initialized'
            else:
                action = 'unchanged'
            # 5.4 原故障项恢复稳定且内容未变时恢复 discovered；内容已变时保留上面的 stale。
            if not content_changed and row['status'] in ('failed', 'unstable', 'unavailable', 'unsupported'):
                row['status'] = 'discovered'
        # 5.5 刷新当前路径和元数据；内容不变但属性或状态有变化时单独记录元数据更新。
        row.update(relpath=item['relpath'], dev=item['st_dev'], ino=item['st_ino'],
                   size=item['size'], mtime_ns=item['mtime_ns'], ctime_ns=item['ctime_ns'],
                   sha256=item['sha256'])
        if action == 'unchanged' and row != previous:
            action = 'metadata_updated'
        # 5.6 保存待写完整行和便于展示的动作摘要，累计本次处理数量，尚不执行 SQL。
        records.append(row)
        changes.append(IdentityChange(
            file_id=row['id'], relpath=row['relpath'], action=action,
            content_rev=row['content_rev'], path_rev=row['path_rev'],
            status=row['status'], sha256=row['sha256'],
            old_relpath=previous['relpath'] if renamed else None))
        counts[action] += 1
    # 6. 旧身份未被任何新观察认领，且旧路径已确认消失，才加入 missing；改名保号的旧行不会误入。
    missing = [MissingFile(file_id=row['id'], relpath=row['relpath']) for row in active
               if row['id'] not in used and row['relpath'] in confirmed_missing]
    counts['missing'] = len(missing)
    # 7. 返回身份计划，由外层扫描器复核来源后在事务中统一应用。
    return IdentityPlan(records=records, changes=changes, missing=missing, counts=counts)
