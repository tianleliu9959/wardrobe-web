#!/usr/bin/env python3
"""C-1 推送：把本地参考库导出为 JSON 并 POST 到云端 /api/admin/push-ref。

用法：
  CLOUD_URL=https://xxx.onrender.com ADMIN_TOKEN=xxxx python push_ref.py [本地db路径]

参考表 = 除用户数据表(backup / wishlist / owned_log / upload_record)外的全部表。
云端会保留已有的 owned 标记；云端为空(首次)时用本地的 owned 作为种子。
"""
import sqlite3
import sys
import os
import json
import urllib.request
import urllib.error

LOCAL_DB = sys.argv[1] if len(sys.argv) > 1 else "wardrobe.db"
CLOUD_URL = os.environ.get("CLOUD_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

SKIP_KEYS = ("backup", "wishlist", "owned_log", "upload_record")


def export_ref(local_db):
    """导出参考库表结构+数据，以及 owned 种子。返回 (tables, owned_seed)。"""
    conn = sqlite3.connect(local_db)
    conn.row_factory = sqlite3.Row
    tables = {}
    for name, create_sql in conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        low = name.lower()
        if any(k in low for k in SKIP_KEYS):
            continue
        cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({name})").fetchall()]
        rows = []
        for r in conn.execute(f"SELECT * FROM {name}"):
            rows.append([
                None if v is None else (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                for v in r
            ])
        tables[name] = {"create_sql": create_sql, "columns": cols, "rows": rows}
    owned_seed = {r["id"]: r["owned"] for r in conn.execute("SELECT id, owned FROM outfit_item")}
    conn.close()
    return tables, owned_seed


def main():
    if not CLOUD_URL:
        print("请设置环境变量 CLOUD_URL（云端地址），例如：")
        print("  CLOUD_URL=https://xxx.onrender.com ADMIN_TOKEN=xxxx python push_ref.py")
        sys.exit(1)

    tables, owned_seed = export_ref(LOCAL_DB)
    payload = json.dumps({"tables": tables, "owned": owned_seed}).encode("utf-8")
    req = urllib.request.Request(
        f"{CLOUD_URL}/api/admin/push-ref",
        data=payload,
        headers={"Content-Type": "application/json", "X-Admin-Token": ADMIN_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            print("云端返回：", resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("推送失败：", e.code, e.read().decode("utf-8"))


if __name__ == "__main__":
    main()
