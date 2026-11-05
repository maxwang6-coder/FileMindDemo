"""创建或迁移本机数据库，并按需登记本机配置授权的根。"""

import argparse
import sqlite3

from app.config import load_settings
from app.db import initialize_database
from app.process_lock import data_directory_lock


# 解析本机授权参数，持进程锁创建或迁移数据库并按需登记根。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--root-path')
    parser.add_argument('--root-id', default='demo')
    args = parser.parse_args()
    config_args = []
    for flag, value in (('--config', args.config), ('--root-path', args.root_path)):
        if value is not None:
            config_args.extend([flag, value])
    try:
        configuration = load_settings(config_args)
        with data_directory_lock(configuration.DATA_DIR):
            initialize_database(configuration, root_id=args.root_id)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f'Initialization failed: {error}\n')
    print('Database schema version 1 initialized; existing records preserved.')


if __name__ == '__main__':
    main()
