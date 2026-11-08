"""owned 变更日志：所有标记 owned 的入口写一条记录，便于回溯"某天标了哪些"。

source 取值约定：
  manual    页面手动标记（POST /api/owned）
  recognize AI 识别自动标记（截图/录屏 OCR）
  crawl_set 爬虫 --owned 参数标记
  reset     清空全部 owned 标记（item_id 为 NULL 的汇总记录）
"""
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "wardrobe.db"

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS owned_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    owned INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""


def ensure_table(conn=None):
    """确保 owned_log 表存在；conn 为空时自建临时连接（供启动时调用）。"""
    if conn is None:
        c = sqlite3.connect(DB)
        c.execute(TABLE_SQL)
        c.commit()
        c.close()
    else:
        conn.execute(TABLE_SQL)


def log_owned(conn, item_id, owned, source):
    """写一条 owned 变更日志（item_id 为 None 表示整表批量操作，如 reset）。"""
    conn.execute(
        "INSERT INTO owned_log (item_id, owned, source) VALUES (?, ?, ?)",
        (item_id, 1 if owned else 0, source),
    )
