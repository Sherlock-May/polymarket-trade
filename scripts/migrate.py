"""初始化/升级 SQLite；不访问网络。未来迁移按 schema_version 顺序添加。"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.database import Database
from bot.lock import ProcessLock

p = argparse.ArgumentParser()
p.add_argument("database", nargs="?", default="data/paper.db")
args = p.parse_args()
with ProcessLock(args.database):
    db = Database(args.database)
    print("schema_version=", db.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    db.close()
