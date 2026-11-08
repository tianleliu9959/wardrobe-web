"""《以闪亮之名》本地衣橱管理系统 - Flask 后端。

功能：
- 套装/部件浏览、筛选、排序、分页
- 截图 OCR 识别部件并标记已拥有
- 录屏 OCR 识别部件（SSE 流式进度）
- 部件已拥有状态管理
- 别名/来源编辑
"""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

# 自动加载项目根目录 .env（API keys 持久化，避免每次重置）
def _load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
_load_env()

from flask import (Flask, Response, jsonify, render_template, request,
                     send_from_directory, stream_with_context, session, redirect)

BASE = Path(__file__).parent
DB = Path(os.environ.get("DB_PATH", str(BASE / "wardrobe.db")))
UPLOAD_DIR = BASE / "uploads"
CACHE_DIR = BASE / "cache"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

UPLOAD_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

from owned_log import ensure_table, log_owned

ensure_table()


def cleanup_stale_uploads(max_age_hours=24):
    """启动时清理 uploads 下的纯临时产物，避免无限累积。

    只删明确的一次性临时文件，绝不碰功能缓存：
      - upload_tmp*      : OCR 一次性上传截图（已停用，但历史可能残留）
      - .videoframes/    : 视频抽帧临时目录（处理完未清理会累积）
    不删除：识别缓存原图(int.png)、裁图(crop_*.png)、upload_record 关联的参考图
            （这些由 use_file / 用户显式删除管理，误删会破坏识别复用与用户数据）。
    """
    try:
        import time as _t
        cutoff = _t.time() - max_age_hours * 3600
        # 临时前缀（不含识别缓存）
        tmp_prefixes = ("upload_tmp",)
        removed = 0
        for p in UPLOAD_DIR.iterdir():
            if not p.exists():
                continue
            if p.is_dir():
                # 仅清理视频帧临时目录
                if p.name == ".videoframes" and p.stat().st_mtime < cutoff:
                    import shutil as _sh
                    _sh.rmtree(p, ignore_errors=True)
                    removed += 1
                continue
            if p.name.startswith(tmp_prefixes) and p.stat().st_mtime < cutoff:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"[cleanup] 清理 {removed} 个过期临时文件", flush=True)
    except Exception as e:
        print(f"[cleanup] 清理失败（忽略）: {e}", flush=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512MB

# ---- 部署/安全相关配置（全部走环境变量，默认值保持本地行为不变）----
APP_PASSWORD = os.environ.get("APP_PASSWORD")            # 设置后启用网页登录鉴权；不设=不鉴权(本地)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN") or APP_PASSWORD  # 推送参考库用的管理令牌
SESSION_SECRET = os.environ.get("SESSION_SECRET") or "dev-insecure-change-me"
RECOGNIZE_ENABLED = os.environ.get("RECOGNIZE_ENABLED", "true").lower() == "true"
app.secret_key = SESSION_SECRET

# 内存管控：非 OCR 请求处理完后立即释放 PaddleOCR 模型，避免常驻内存。
# 仅当模型存在、且当前请求没有标记为正在使用 OCR 时才释放。
@app.after_request
def _release_ocr_after_request(resp):
    # 页面/接口一律不缓存，保证模板改动即时生效（本地开发无 CDN 成本）
    resp.headers["Cache-Control"] = "no-store"
    if not _ocr_busy:
        release_ocr()
    return resp


@app.before_request
def _access_guard():
    # 1) 识别功能开关（云端可关闭；本地默认开启）
    if "/recognize" in request.path and not RECOGNIZE_ENABLED:
        if request.path.startswith("/api/"):
            return jsonify({"error": "识别功能未启用"}), 503
        return "识别功能未启用", 503
    # 2) 网页登录鉴权（仅当设置了 APP_PASSWORD 才启用，否则保持本地无鉴权行为）
    if not APP_PASSWORD:
        return
    if request.path in ("/login", "/logout") or request.path.startswith(("/static", "/uploads", "/api/admin/")):
        return
    if session.get("user"):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")

# 用户确认「实际没有」的部件黑名单（避免误标）
EXCLUDED_ITEMS = {
    "幻光星轨",   # 梦遐绮旅·戒指，用户确认没有
    "指尖月光",   # 白纱花祈·戒指，用户确认没有
    "鸢尾环梦",   # 鸢尾与权杖·发饰，用户确认没有
}

# 截图中的非部件噪音文字（部位 tab / 筛选 / 风格评级 / 界面词）
NOISE_WORDS = {
    # 部位 tab
    "发型", "发饰", "帽子", "耳饰", "面饰", "颈饰", "外套", "上衣", "连衣裙",
    "下装", "袜子", "鞋子", "手持物", "腕饰", "手套", "戒指", "背饰", "翅膀",
    "尾巴", "悬浮", "斜挎", "纹身", "脚链", "座驾", "美瞳", "唇妆", "眼影",
    "腮红", "眉妆", "睫毛", "眼线", "纹面", "甲油", "甲型",
    # 筛选/界面
    "全部", "已获得", "未获得", "已拥有", "未拥有", "搜索", "收集度",
    # 风格评级
    "典雅", "清纯", "简约", "跃动", "性感", "华丽", "酷帅", "甜美", "清凉",
    # 其它
    "时装", "套装", "散件", "部位", "风格", "星级",
}

# 非部件文字判断：是否应排除
def is_noise(text):
    """判断 OCR 文本是否为非部件噪音。"""
    t = (text or "").strip()
    if not t:
        return True
    # 含数字/符号/字母的（如 19/212, 918075+, ★个个）
    if re.search(r"\d", t):
        return True
    # 含特殊符号
    if re.search(r"[+*/%#★☆√]", t):
        return True
    # 长度不在 2-6 字（部件名通常 2-6 个中文字）
    if not (2 <= len(t) <= 6):
        return True
    # 在黑名单中
    if t in NOISE_WORDS:
        return True
    # 含英文/数字（非纯中文）
    if not re.fullmatch(r"[\u4e00-\u9fff]+", t):
        return True
    return False

# PaddleOCR 单例（惰性初始化）
_ocr = None
_ocr_busy = False   # 标记当前请求是否正在使用 OCR，避免被 after_request 误释放


def get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ocr


def release_ocr():
    """释放 PaddleOCR 模型及其底层框架占用的内存（这是内存爆炸的主因）。

    PaddlePaddle 模型常驻内存可达 2-3GB。Web 服务只有上传截图/视频时才用到
    OCR，其余时间应尽快释放，避免模型一直霸占内存。
    """
    global _ocr
    if _ocr is None:
        return
    try:
        _ocr = None
        import gc
        gc.collect()
    except Exception:
        pass


from contextlib import contextmanager


@contextmanager
def _ocr_guard():
    """OCR 使用上下文：标记 busy，防止 after_request 误释放正在使用的模型。"""
    global _ocr_busy
    _ocr_busy = True
    try:
        yield
    finally:
        _ocr_busy = False


# ---------- 内存监控告警 ----------
# 内存占用超过该百分比时自动释放 OCR 并打印告警（防止再次因内存爆炸被关停）
_MEM_WARN_PCT = 75.0
_MEM_CRIT_PCT = 85.0


def _mem_percent():
    """当前进程内存占物理内存的百分比（约数），取不到时返回 0。

    注意单位：Linux 的 ru_maxrss 是 KB，macOS 的 ru_maxrss 是 bytes；
    total 统一用 bytes，故 Linux 下需将 rss *1024 转为 bytes，否则比例会缩小 1024 倍，
    导致内存告警几乎永远不触发（原 bug）。
    """
    try:
        import resource
        import sys as _sys
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if _sys.platform.startswith("linux"):
            rss *= 1024  # KB -> bytes
        total = 0
        if _sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:      # Linux
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) * 1024  # KB -> bytes
                        break
        else:
            import subprocess
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True
            ).stdout.strip()                   # macOS
            total = int(out) if out.isdigit() else 0
        return (rss / total * 100.0) if total else 0.0
    except Exception:
        return 0.0


def check_memory(force_release=True):
    """检查进程内存；超阈值时释放 OCR 并返回告警消息，否则返回 None。"""
    try:
        import psutil
        pct = psutil.Process().memory_percent()
    except Exception:
        pct = _mem_percent()
    if pct >= _MEM_CRIT_PCT:
        release_ocr()
        return f"[内存告警-严重] 进程内存占用 {pct:.1f}%，已强制释放 OCR 模型，请检查服务负载"
    if pct >= _MEM_WARN_PCT:
        if force_release:
            release_ocr()
        return f"[内存告警-预警] 进程内存占用 {pct:.1f}%，已释放 OCR 模型"
    return None


@app.before_request
def _check_mem_before_request():
    """每个请求开始前检查一次内存，超高时告警。OCR busy 时只告警不释放。"""
    global _ocr_busy
    try:
        if _ocr_busy:
            return  # 正在用 OCR，不能释放，仅跳过本次检查
        msg = check_memory()
        if msg:
            app.logger.warning(msg)
    except Exception:
        pass


def get_db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL 减少读写互斥；synchronous=NORMAL 提升写吞吐（崩溃最多丢最近一次提交，可接受）
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _form_map(conn):
    """多形态映射：item_id -> [{form_no, image, image_big}, ...]（form_no 升序）。

    outfit_item_forms 由爬虫/补录脚本维护，是前端形态数据的唯一来源。
    无多形态的部件返回空数组（接口层统一处理，页面无需再判空）。
    """
    m = {}
    for r in conn.execute(
        "SELECT item_id, form_no, image, image_big FROM outfit_item_forms ORDER BY item_id, form_no"
    ).fetchall():
        m.setdefault(r["item_id"], []).append({
            "form_no": r["form_no"],
            "image": r["image"],
            "image_big": r["image_big"],
        })
    return m


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["user"] = "1"
            return redirect("/")
        return render_template("login.html", error="密码错误")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")


@app.route("/api/admin/push-ref", methods=["POST"])
def admin_push_ref():
    """管理接口：本地推送参考库到云端，保留云端已有的 owned 标记。
    鉴权：设置了 ADMIN_TOKEN 则要求请求头 X-Admin-Token 匹配；未设置则放开(本地)。"""
    token = request.headers.get("X-Admin-Token") or (request.get_json(silent=True) or {}).get("token")
    if ADMIN_TOKEN is not None and token != ADMIN_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    tables = data.get("tables")
    if not isinstance(tables, dict):
        return jsonify({"error": "missing tables"}), 400
    conn = get_db()
    try:
        snap = {r["id"]: r["owned"] for r in conn.execute("SELECT id, owned FROM outfit_item")}
    except sqlite3.OperationalError:
        snap = {}
    conn.execute("PRAGMA foreign_keys=OFF")
    for tname, tdata in tables.items():
        create_sql = tdata.get("create_sql")
        cols = tdata.get("columns") or []
        rows = tdata.get("rows") or []
        if create_sql:
            conn.execute(f"DROP TABLE IF EXISTS {tname}")
            conn.execute(create_sql)
        if cols and rows:
            ph = ",".join("?" * len(cols))
            conn.executemany(
                f"INSERT INTO {tname} ({','.join(cols)}) VALUES ({ph})", rows
            )
    # owned 基准：优先保留云端已有标记；云端为空(首次)时用本地作为种子
    base = snap if snap else (data.get("owned") or {})
    for iid, owned in base.items():
        conn.execute("UPDATE outfit_item SET owned=? WHERE id=?", (owned, iid))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "preserved_owned": len(base), "tables": list(tables.keys())})


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """提供上传文件的静态访问。"""
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/set/<int:set_id>")
def set_detail_page(set_id):
    """套装详情独立页面。"""
    conn = get_db()
    s = conn.execute(
        "SELECT id, name, alias, attribute, preview, obtain, version, star FROM outfit_set WHERE id=?",
        (set_id,),
    ).fetchone()
    if not s:
        conn.close()
        return "套装不存在", 404
    items = [dict(i) for i in conn.execute(
        "SELECT id, name, type, star, image, image_big, owned FROM outfit_item WHERE set_id=? ORDER BY id",
        (set_id,),
    ).fetchall()]
    fm = _form_map(conn)
    for it in items:
        it["forms"] = fm.get(it["id"], [])
    # 心标状态
    wished = set(r[0] for r in conn.execute("SELECT item_id FROM wishlist").fetchall())
    conn.close()
    return render_template("set_detail.html", s=dict(s), items=items, wished=wished)


@app.route("/api/set/<int:set_id>")
def api_set_detail(set_id):
    """套装详情 JSON 接口（识别页弹窗内容级渲染用）。"""
    conn = get_db()
    s = conn.execute(
        "SELECT id, name, alias, attribute, preview, obtain, version, star FROM outfit_set WHERE id=?",
        (set_id,),
    ).fetchone()
    if not s:
        conn.close()
        return jsonify({"error": "套装不存在"}), 404
    items = [dict(i) for i in conn.execute(
        "SELECT id, name, type, star, image, image_big, owned FROM outfit_item WHERE set_id=? ORDER BY id",
        (set_id,),
    ).fetchall()]
    fm = _form_map(conn)
    for it in items:
        it["forms"] = fm.get(it["id"], [])
    wished = sorted(r[0] for r in conn.execute("SELECT item_id FROM wishlist").fetchall())
    conn.close()
    return jsonify({"set": dict(s), "items": items, "wished": wished})


# 部位下拉框顺序：先衣服主体，再头部/脸部装饰，再其它配饰（从头到脚）
TYPE_ORDER = [
    "发型", "连衣裙", "外套", "上衣", "下装", "袜子", "鞋子",
    "帽子", "发饰", "面饰",
    "颈饰", "耳饰", "腕饰", "戒指", "手套", "脚链", "斜挎", "手持物",
    "背饰", "翅膀", "纹身", "尾巴", "悬浮", "座驾",
]

@app.route("/recognize")
def recognize_page():
    """AI 识别独立页面。"""
    conn = get_db()
    present = {r[0] for r in conn.execute("SELECT DISTINCT type FROM outfit_item WHERE type IS NOT NULL AND type != ''").fetchall()}
    conn.close()
    # 已存在的类型按固定分类顺序排，数据库中新增的未知类型按原样补到末尾
    types = [t for t in TYPE_ORDER if t in present] + sorted(present - set(TYPE_ORDER))
    return render_template("recognize.html", types=types)


@app.route("/wishlist")
def wishlist_page():
    """心愿单独立页面。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT w.item_id, i.name, i.type, i.star, i.owned, i.image, i.image_big, "
        "i.set_id, s.name AS set_name, s.alias AS set_alias "
        "FROM wishlist w JOIN outfit_item i ON i.id = w.item_id "
        "LEFT JOIN outfit_set s ON i.set_id = s.id ORDER BY w.id DESC"
    ).fetchall()
    conn.close()
    return render_template("wishlist.html", items=[dict(r) for r in rows])


@app.route("/check-unmatched")
def check_unmatched():
    """人工检查「本轮未再匹配」的部件（上次标记过、本轮未匹配上）。"""
    return render_template("check_unmatched.html")


@app.route("/check-loose")
def check_loose():
    """人工确认「免活」来源但未齐全的套装（存在未拥有部件）。"""
    return render_template("check_loose.html")


# api_sets 全量结果缓存：owned/alias/obtain/新增散件会失效；forms 字段随
# outfit_item_forms 变化，爬虫/补录脚本改库后本进程缓存不自动失效，需重启（或清缓存）
_sets_cache = None


def _invalidate_sets_cache():
    global _sets_cache
    _sets_cache = None


@app.route("/api/sets")
def api_sets():
    global _sets_cache
    if _sets_cache is not None:
        return jsonify(_sets_cache)
    conn = get_db()
    # 只 SELECT 需要的字段，避免 SELECT * 构造多余 Row 的内存开销
    sets = conn.execute(
        "SELECT id, name, alias, attribute, preview, obtain, gacha, version, star "
        "FROM outfit_set ORDER BY id"
    ).fetchall()
    items = conn.execute(
        "SELECT id, name, attribute, type, star, version, image, image_big, obtain, owned, set_id "
        "FROM outfit_item ORDER BY id"
    ).fetchall()
    fm = _form_map(conn)
    conn.close()

    by_set = {}
    loose = []   # 散件（set_id 为空）
    for it in items:
        item = {
            "id": it["id"],
            "name": it["name"],
            "attribute": it["attribute"],
            "type": it["type"],
            "star": it["star"],
            "version": it["version"],
            "image": it["image"],
            "image_big": it["image_big"],
            "obtain": it["obtain"],
            "owned": it["owned"],
            "forms": fm.get(it["id"], []),
        }
        if it["set_id"] is None:
            loose.append(item)
        else:
            by_set.setdefault(it["set_id"], []).append(item)

    result = []
    for s in sets:
        result.append({
            "id": s["id"],
            "name": s["name"],
            "alias": s["alias"],
            "attribute": s["attribute"],
            "preview": s["preview"],
            "obtain": s["obtain"],
            "gacha": s["gacha"],
            "version": s["version"],
            "star": s["star"],
            "items": by_set.get(s["id"], []),
        })

    _sets_cache = {"sets": result, "loose": loose}
    return jsonify(_sets_cache)


@app.route("/api/reset-owned", methods=["POST"])
def api_reset_owned():
    """清空所有 owned 标记（去除上一次匹配结果）。"""
    conn = get_db()
    conn.execute("UPDATE outfit_item SET owned=0")
    log_owned(conn, None, 0, "reset")
    conn.commit()
    conn.close()
    _invalidate_sets_cache()
    return jsonify({"ok": True})


@app.route("/api/clear-cache", methods=["POST"])
def api_clear_cache():
    """清除视频 OCR 缓存。"""
    n = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return jsonify({"ok": True, "cleared": n})


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    n_set = conn.execute("SELECT COUNT(*) FROM outfit_set").fetchone()[0]
    n_item = conn.execute("SELECT COUNT(*) FROM outfit_item").fetchone()[0]
    n_owned = conn.execute("SELECT COUNT(*) FROM outfit_item WHERE owned=1").fetchone()[0]
    n_owned_set = conn.execute(
        "SELECT COUNT(DISTINCT set_id) FROM outfit_item WHERE owned=1"
    ).fetchone()[0]
    conn.close()
    return jsonify({
        "set": n_set,
        "item": n_item,
        "owned": n_owned,
        "owned_set": n_owned_set,
    })


@app.route("/api/owned", methods=["POST"])
def api_owned():
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    owned = 1 if data.get("owned") else 0
    if item_id is None:
        return jsonify({"error": "缺少 item_id"}), 400
    conn = get_db()
    conn.execute("UPDATE outfit_item SET owned=? WHERE id=?", (owned, item_id))
    log_owned(conn, item_id, owned, "manual")
    conn.commit()
    conn.close()
    _invalidate_sets_cache()
    return jsonify({"ok": True})


@app.route("/api/owned/log")
def api_owned_log():
    """最近 owned 变更日志（owned_log），按时间倒序。

    query:
      type  部位过滤（可选，如 耳饰）
      limit 条数（默认 20，最大 200）

    用于回溯「最近拥有了哪些部件」。owned=0 表示取消标记；
    item_id 为 NULL 的是整表批量操作（如 reset 清空全部 owned）。
    """
    type_filter = (request.args.get("type") or "").strip() or None
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 200))

    conn = get_db()
    sql = (
        "SELECT l.created_at, l.owned, l.source, i.id, i.name, i.type, i.star, "
        "i.attribute, i.image, s.name AS set_name "
        "FROM owned_log l LEFT JOIN outfit_item i ON i.id = l.item_id "
        "LEFT JOIN outfit_set s ON s.id = i.set_id"
    )
    args = []
    if type_filter:
        sql += " WHERE i.type = ?"
        args.append(type_filter)
    sql += " ORDER BY l.created_at DESC, l.id DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return jsonify({"items": rows})


@app.route("/api/alias", methods=["POST"])
def api_alias():
    data = request.get_json(silent=True) or {}
    set_id = data.get("set_id")
    alias = (data.get("alias") or "").strip() or None
    if set_id is None:
        return jsonify({"error": "缺少 set_id"}), 400
    conn = get_db()
    obtain = conn.execute("SELECT obtain FROM outfit_set WHERE id=?", (set_id,)).fetchone()
    if obtain is None:
        conn.close()
        return jsonify({"error": "套装不存在"}), 404
    if not (obtain[0] or "").startswith("限时"):
        conn.close()
        return jsonify({"error": "仅限时追光套装可填写别名"}), 403
    conn.execute("UPDATE outfit_set SET alias=? WHERE id=?", (alias, set_id))
    conn.commit()
    conn.close()
    _invalidate_sets_cache()
    return jsonify({"ok": True})


@app.route("/api/obtain", methods=["POST"])
def api_obtain():
    data = request.get_json(silent=True) or {}
    set_id = data.get("set_id")
    obtain = (data.get("obtain") or "").strip() or None
    if set_id is None:
        return jsonify({"error": "缺少 set_id"}), 400
    conn = get_db()
    conn.execute("UPDATE outfit_set SET obtain=? WHERE id=?", (obtain, set_id))
    conn.commit()
    conn.close()
    _invalidate_sets_cache()
    return jsonify({"ok": True})


def _recognize(path):
    with _ocr_guard():
        ocr = get_ocr()
        result = ocr.ocr(path, cls=True)
        texts = []
        if result and result[0]:
            for box, (text, _score) in result[0]:
                t = (text or "").strip()
                if t:
                    texts.append(t)
        return texts


# 部件名索引：按「是否精确名」缓存 + 按长度分桶，加速模糊匹配
_name_index = None


def _get_name_index():
    global _name_index
    if _name_index is not None:
        return _name_index
    conn = get_db()
    rows = conn.execute("SELECT id, name, type FROM outfit_item").fetchall()
    conn.close()

    exact = {}                  # name -> [(id, type)]
    by_len = {}                 # len -> [name]（全类型）
    by_type_len = {}            # type -> {len -> [name]}（按类型分桶）
    all_types = set()
    for r in rows:
        name = r["name"]
        typ = r["type"]
        exact.setdefault(name, []).append((r["id"], typ))
        by_len.setdefault(len(name), []).append(name)
        by_type_len.setdefault(typ, {}).setdefault(len(name), []).append(name)
        all_types.add(typ)

    for k in by_len:
        by_len[k] = list(set(by_len[k]))
    for t, d in by_type_len.items():
        for k in d:
            d[k] = list(set(d[k]))

    _name_index = {"exact": exact, "by_len": by_len, "by_type_len": by_type_len}
    return _name_index


def _match_one(t, idx, type_filter=None):
    """单个文本匹配部件名，返回名字或 None。保守策略：宁漏不误。

    type_filter: 限定部位（如「颈饰」），匹配时只在对应部位内搜索。
    匹配优先级：
      1) 精确命中（含部位校验）
      2) 高相似度编辑距离纠正（cutoff 0.7）
      3) 短碎片前缀唯一匹配
      4) 长碎片前缀匹配
    """
    import difflib

    if type_filter:
        if t in idx["exact"]:
            if t in EXCLUDED_ITEMS:
                return None
            for _id, typ in idx["exact"][t]:
                if typ == type_filter:
                    return t
            return None
    elif t in idx["exact"]:
        if t in EXCLUDED_ITEMS:
            return None
        return t

    bucket = None
    if type_filter:
        bucket = idx["by_type_len"].get(type_filter)
        if bucket is None:
            return None
    else:
        bucket = idx["by_len"]

    L = len(t)
    candidates = []
    for d in range(-2, 3):
        candidates.extend(bucket.get(L + d, []))

    if not candidates:
        return None

    # A) 完整词形近纠正（双方>=3字，ratio>=0.7）
    if len(t) >= 3:
        m = difflib.get_close_matches(t, candidates, n=1, cutoff=0.7)
        if m and m[0] not in EXCLUDED_ITEMS:
            return m[0]

    # B) 短碎片（1-2字）：仅当部位内「前缀候选唯一」才匹配
    if len(t) <= 2:
        prefix = [n for n in candidates if n.startswith(t) and n != t and n not in EXCLUDED_ITEMS]
        if len(prefix) == 1:
            return prefix[0]
        return None

    # C) 长碎片前缀匹配（3字+）
    if L >= 3:
        min_common = L - 1
        prefix_long = []
        for n in candidates:
            if n == t or abs(len(n) - L) > 2 or n in EXCLUDED_ITEMS:
                continue
            common = 0
            for a, b in zip(t, n):
                if a != b:
                    break
                common += 1
            if common >= min_common:
                prefix_long.append(n)
        if len(prefix_long) == 1:
            return prefix_long[0]

    return None


def _match_and_mark(texts, progress=None, type_filter=None):
    """把 OCR 文本匹配到部件名并标记 owned=1，返回 (matched, unmatched)。

    第一遍纯内存匹配；第二遍对命中名字批量 IN 查询 meta + 批量 UPDATE owned，
    避免逐条 SELECT/UPDATE 带来的 N 次查询开销。
    """
    conn = get_db()
    idx = _get_name_index()

    unique_texts = list(dict.fromkeys(texts))
    total = len(unique_texts)

    # 1) 内存匹配，得到每个文本的 best 名字
    best_by_text = []   # [(text, best_name or None)]
    unmatched = []
    for i, t in enumerate(unique_texts):
        if progress and (i % 20 == 0 or i == total - 1):
            progress(i + 1, total)
        best = _match_one(t, idx, type_filter=type_filter)
        if not best:
            unmatched.append(t)
            best_by_text.append((t, None))
        else:
            best_by_text.append((t, best))

    # 2) 批量查询所有命中名字的 meta（一次 IN 查询）
    hit_names = list(dict.fromkeys(b for _, b in best_by_text if b))
    rows_by_name = {}
    if hit_names:
        ph = ",".join("?" * len(hit_names))
        rows = conn.execute(
            "SELECT i.id, i.name, i.type, i.attribute, s.name AS set_name "
            "FROM outfit_item i JOIN outfit_set s ON i.set_id = s.id "
            f"WHERE i.name IN ({ph})",
            hit_names,
        ).fetchall()
        for r in rows:
            rows_by_name.setdefault(r["name"], []).append(r)

    # 3) 决定要标 owned 的名字（type_filter 下至少命中一行）
    mark_names = []
    for name in hit_names:
        rs = rows_by_name.get(name, [])
        if type_filter:
            rs = [r for r in rs if r["type"] == type_filter]
            if not rs:
                continue
        mark_names.append(name)

    # 4) 批量标记 owned
    if mark_names:
        pm = ",".join("?" * len(mark_names))
        rows = conn.execute(f"SELECT id FROM outfit_item WHERE name IN ({pm})", mark_names).fetchall()
        conn.execute(f"UPDATE outfit_item SET owned=1 WHERE name IN ({pm})", mark_names)
        for r in rows:
            log_owned(conn, r["id"], 1, "recognize")

    # 5) 按原顺序组装 matched
    matched = []
    for t, best in best_by_text:
        if best is None or best not in rows_by_name:
            continue
        rs = rows_by_name[best]
        if type_filter:
            rs = [r for r in rs if r["type"] == type_filter]
        for r in rs:
            matched.append({
                "ocr": t,
                "name": r["name"],
                "type": r["type"],
                "attribute": r["attribute"],
                "set": r["set_name"],
                "item_id": r["id"],
                "corrected": t != r["name"],
            })

    conn.commit()
    conn.close()
    _invalidate_sets_cache()
    return matched, unmatched


def _video_hash(path):
    """计算视频文件内容的 SHA256，作为缓存 key。"""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_get(key):
    fp = CACHE_DIR / f"{key}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_set(key, data):
    fp = CACHE_DIR / f"{key}.json"
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _coarse_cache_key(orig_path, types, box):
    """同图同部件粗排缓存 key：原图内容 hash + 部位 + 归一化框坐标。

    box 必须存在（[x1,y1,x2,y2]），否则无法判定是否同框，返回 None 表示不缓存。
    """
    if not box or not orig_path:
        return None
    try:
        import hashlib
        with open(orig_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None
    b = "_".join(str(int(round(float(x)))) for x in box)
    return f"coarse_{h}_{types}_{b}"


def _coarse_cache_clear():
    """新散件入库后粗排结果可能变化，清空粗排缓存。"""
    try:
        for f in CACHE_DIR.glob("coarse_*.json"):
            f.unlink()
    except OSError:
        pass


def _extract_video_frames(video_path, workdir, fps=3):
    import subprocess
    out = str(workdir / "f_%04d.jpg")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", video_path, "-vf", f"fps={fps}", "-q:v", "3", out],
        check=True, capture_output=True, timeout=600,
    )
    return sorted(workdir.glob("f_*.jpg"))


def _dhash(frame_path, size=8):
    from PIL import Image
    img = Image.open(frame_path).convert("L").resize((size + 1, size), Image.LANCZOS)
    diff = []
    px = img.load()
    for y in range(size):
        for x in range(size):
            diff.append(px[x, y] > px[x + 1, y])
    bits = [1 if d else 0 for d in diff]
    return int("".join(str(b) for b in bits), 2)


def _dedupe_frames(frames, threshold=8):
    """dHash 去重：相邻帧相似度太高则跳过，避免重复 OCR。"""
    import statistics
    keep = []
    prev_hash = None
    for fp in frames:
        try:
            h = _dhash(fp)
        except Exception:
            continue
        if prev_hash is None or bin(h ^ prev_hash).count("1") >= threshold:
            keep.append(fp)
            prev_hash = h
    return keep


def _recognize_video(path, progress=None):
    """识别视频，返回 (去重后的文本列表, 保留帧数)。使用缓存。"""
    key = _video_hash(path)
    cached = _cache_get(key)
    if cached is not None:
        if progress:
            progress("cache", 1, 1, "命中缓存，直接使用上次识别结果")
        return cached["texts"], cached.get("frames", 0)

    import shutil as _sh
    workdir = BASE / ".videoframes" / str(int(time.time() * 1000))
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        if progress:
            progress("extract", 0, 0, "正在抽帧…")
        frames = _extract_video_frames(path, workdir)
        frames = _dedupe_frames(frames)

        all_texts = []
        total = len(frames)
        with _ocr_guard():
            ocr = get_ocr()
            for i, fp in enumerate(frames):
                if progress:
                    progress("ocr", i + 1, total, f"OCR 识别帧 {i+1}/{total}")
                result = ocr.ocr(str(fp), cls=True)
                if result and result[0]:
                    for box, (text, _score) in result[0]:
                        t = (text or "").strip()
                        if t:
                            all_texts.append(t)
                # 用完立即释放 result 引用，并每 20 帧强制 GC 一次
                # 避免 OCR 内部缓存的图像帧引用堆积
                del result
                if (i + 1) % 20 == 0:
                    import gc as _gc
                    _gc.collect()

        # 去重保序
        seen = set()
        texts = []
        for t in all_texts:
            if t not in seen:
                seen.add(t)
                texts.append(t)

        _cache_set(key, {"texts": texts, "frames": len(frames)})
        return texts, len(frames)
    finally:
        try:
            _sh.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """截图识别（限定部位）。

    工作流：
    1. 过滤非部件噪音文字
    2. 识别到的部件名：
       - 数据库已有 → 标记 owned（matched）
       - 数据库没有 → 插入为散件(set_id空) + 标记 owned（new_loose，供用户确认）
    3. 返回 missed_owned：该部位数据库 owned 但本次截图未识别到（供用户确认）
    """
    if "file" not in request.files:
        return jsonify({"error": "未收到文件"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return jsonify({"error": "仅支持图片 (jpg/png/webp/bmp)"}), 400

    type_filter = request.form.get("type") or ""
    if not type_filter:
        return jsonify({"error": "请先选择「限定部位」"}), 400

    # 用固定文件名覆盖保存，避免累积；不删除（环境拦截删除操作）
    path = UPLOAD_DIR / f"upload_tmp{suffix}"
    f.save(path)

    try:
        # 识别前：该部位当前 owned 的部件名（作为基线）
        conn = get_db()
        before_rows = conn.execute(
            "SELECT name FROM outfit_item WHERE type=? AND owned=1", (type_filter,)
        ).fetchall()
        before_owned = set(r["name"] for r in before_rows)
        conn.close()

        # OCR 识别
        all_texts = _recognize(str(path))
        # 过滤噪音，只保留可能的部件名
        texts = [t for t in all_texts if not is_noise(t)]
        texts = list(dict.fromkeys(texts))  # 去重

        # 匹配现有部件并标记 owned
        matched, unmatched = _match_and_mark(texts, type_filter=type_filter)
        matched_names = set(m["name"] for m in matched)

        # 未匹配的文本：插入为散件（set_id空）+ 标记 owned
        new_loose = []
        conn = get_db()
        for t in unmatched:
            # 已作为散件插入过则跳过
            exist = conn.execute(
                "SELECT id FROM outfit_item WHERE set_id IS NULL AND name=? AND type=?",
                (t, type_filter),
            ).fetchone()
            if exist:
                conn.execute("UPDATE outfit_item SET owned=1 WHERE id=?", (exist["id"],))
                log_owned(conn, exist["id"], 1, "recognize")
                new_loose.append({"item_id": exist["id"], "name": t, "type": type_filter, "new": False})
                continue
            # 插入新散件
            cur = conn.execute(
                "INSERT INTO outfit_item (set_id, name, type, owned) VALUES (NULL, ?, ?, 1)",
                (t, type_filter),
            )
            log_owned(conn, cur.lastrowid, 1, "recognize")
            new_loose.append({"item_id": cur.lastrowid, "name": t, "type": type_filter, "new": True})
        conn.commit()
        conn.close()
        # 新散件已入库：失效名称索引（让后续识别能匹配到新散件）、全量缓存和粗排缓存
        global _name_index
        _name_index = None
        _invalidate_sets_cache()
        _coarse_cache_clear()

        # 该部位「之前 owned 但本次截图未识别到」的部件
        all_hit_names = matched_names | set(x["name"] for x in new_loose)
        missed = []
        if before_owned:
            conn = get_db()
            names = sorted(before_owned - all_hit_names)
            if names:
                rows = conn.execute(
                    "SELECT i.id, i.name, i.type, COALESCE(s.name, '散件') AS set_name "
                    "FROM outfit_item i LEFT JOIN outfit_set s ON i.set_id=s.id "
                    f"WHERE i.type=? AND i.name IN ({','.join('?' * len(names))}) AND i.owned=1",
                    (type_filter, *names),
                ).fetchall()
                for r in rows:
                    missed.append({
                        "item_id": r["id"], "name": r["name"],
                        "type": r["type"], "set": r["set_name"],
                    })
            conn.close()

        return jsonify({
            "texts": texts,
            "matched": matched,
            "new_loose": new_loose,
            "missed_owned": missed,
            "type_filter": type_filter,
        })
    except Exception as e:
        return jsonify({"error": f"识别失败: {e}"}), 500
    finally:
        # 上传的临时截图用完即删（OCR 一次性消费，无需保留；避免 uploads 累积）
        try:
            if path and Path(path).exists():
                Path(path).unlink()
        except OSError:
            pass


@app.route("/api/upload-video", methods=["POST"])
def api_upload_video():
    if "file" not in request.files:
        return jsonify({"error": "未收到文件"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in VIDEO_EXTS:
        return jsonify({"error": "仅支持视频 (mp4/mov/m4v/avi/mkv/webm)"}), 400

    path = UPLOAD_DIR / f"video_upload_{int(time.time() * 1000)}{suffix}"
    f.save(path)

    try:
        texts, kept_frames = _recognize_video(str(path))
        matched, unmatched = _match_and_mark(texts)
        return jsonify({
            "frames": kept_frames,
            "texts": texts,
            "matched": matched,
            "unmatched": unmatched,
        })
    except Exception as e:
        return jsonify({"error": f"识别失败: {e}"}), 500
    finally:
        try:
            path.unlink()
        except OSError:
            pass


@app.route("/api/upload-video-stream", methods=["POST"])
def api_upload_video_stream():
    """SSE 流式录屏识别。"""
    if "file" not in request.files:
        return jsonify({"error": "未收到文件"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in VIDEO_EXTS:
        return jsonify({"error": "仅支持视频 (mp4/mov/m4v/avi/mkv/webm)"}), 400

    type_filter = request.form.get("type") or None
    path = UPLOAD_DIR / f"video_stream_{int(time.time() * 1000)}{suffix}"
    f.save(path)

    def emit(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def gen():
        def progress(phase, done, total, msg):
            # 由外部闭包转发给 SSE
            nonlocal last_phase
            last_phase = (phase, done, total, msg)

        last_phase = None

        try:
            if _cache_get(_video_hash(path)) is not None:
                yield emit({"type": "progress", "msg": "命中缓存，直接使用上次识别结果…"})
            texts, kept = _recognize_video(str(path), progress=lambda p, d, t, m: None)
            yield emit({"type": "result", "frames": kept, "matched": _match_and_mark(texts, type_filter=type_filter)[0]})
        except Exception as e:
            yield emit({"type": "error", "msg": f"识别失败: {e}"})
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/wishlist", methods=["GET"])
def api_wishlist():
    """查看心愿单。返回带照片数的部件列表。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT w.id, w.item_id, w.note, w.created_at, i.name, i.type, i.star, i.owned, "
        "i.image, i.image_big, i.set_id, i.obtain, "
        "s.name AS set_name, s.alias AS set_alias, s.gacha AS set_gacha "
        "FROM wishlist w JOIN outfit_item i ON i.id = w.item_id "
        "LEFT JOIN outfit_set s ON i.set_id = s.id ORDER BY w.id DESC"
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM upload_record WHERE item_id=?", (d["item_id"],)
        ).fetchone()[0]
        d["photo_count"] = cnt
        items.append(d)
    # 全量卡池名单（前端做散件→卡池归属匹配用）
    gachas = [r[0] for r in conn.execute(
        "SELECT DISTINCT gacha FROM outfit_set WHERE gacha IS NOT NULL AND gacha != '' ORDER BY gacha"
    ).fetchall()]
    # 卡池名 → 限时别名展示串（供前端卡池统计面板展示）：
    #   六星限时卡池 -> 取六星限时套装 alias（单个/逗号分隔）
    #   五星限时卡池 -> 取全部五星限时套装 alias 拼接（多个逗号分隔）
    #   其他来源（非限时 / 无 alias） -> 空字符串
    gacha_aliases = {}
    for g in gachas:
        rows = conn.execute(
            "SELECT star, alias FROM outfit_set WHERE gacha=? AND obtain='限时追光'", (g,)
        ).fetchall()
        if not rows:
            continue
        six = [r[1] for r in rows if str(r[0]) == "6" and r[1]]
        five = [r[1] for r in rows if str(r[0]) == "5" and r[1]]
        picked = (",".join(six) if six else ",".join(five)) if (six or five) else ""
        if picked:
            gacha_aliases[g] = picked
    conn.close()
    return jsonify({"items": items, "gachas": gachas, "gacha_aliases": gacha_aliases})


@app.route("/api/gacha-sets")
def api_gacha_sets():
    """按卡池名(gacha)返回该卡池全部套装；仅 5/6 星限时追光卡池附带散件（与首页 openGacha 一致）。"""
    gacha = request.args.get("gacha", "").strip()
    if not gacha:
        return jsonify({"error": "缺少 gacha 参数"}), 400
    conn = get_db()
    sets = conn.execute(
        "SELECT id, name, alias, attribute, preview, obtain, gacha, version, star "
        "FROM outfit_set WHERE gacha=? ORDER BY id", (gacha,)
    ).fetchall()
    if not sets:
        conn.close()
        return jsonify({"sets": [], "loose": []})
    set_ids = [s["id"] for s in sets]
    ph = ",".join("?" * len(set_ids))
    items = conn.execute(
        f"SELECT id, name, attribute, type, star, version, image, image_big, obtain, owned, set_id "
        f"FROM outfit_item WHERE set_id IN ({ph}) ORDER BY id", set_ids
    ).fetchall()
    by_set = {}
    for it in items:
        item = {
            "id": it["id"], "name": it["name"], "attribute": it["attribute"],
            "type": it["type"], "star": it["star"], "version": it["version"],
            "image": it["image"], "image_big": it["image_big"],
            "obtain": it["obtain"], "owned": it["owned"],
        }
        by_set.setdefault(it["set_id"], []).append(item)
    result = []
    for s in sets:
        result.append({
            "id": s["id"], "name": s["name"], "alias": s["alias"],
            "attribute": s["attribute"], "preview": s["preview"],
            "obtain": s["obtain"], "gacha": s["gacha"],
            "version": s["version"], "star": s["star"],
            "items": by_set.get(s["id"], []),
        })
    is_limited_high = any(
        s["obtain"] == "限时追光" and (s["star"] == "5" or s["star"] == "6")
        for s in sets
    )
    loose = []
    if is_limited_high:
        loose = conn.execute(
            "SELECT id, name, attribute, type, star, version, image, image_big, obtain, owned, set_id "
            "FROM outfit_item WHERE set_id IS NULL AND obtain LIKE ? ORDER BY id",
            (f"%{gacha}%",),
        ).fetchall()
        loose = [dict(r) for r in loose]
    conn.close()
    return jsonify({"sets": result, "loose": loose})


@app.route("/api/wishlist/toggle", methods=["POST"])
def api_wishlist_toggle():
    """心愿单 加入/移除。body: {item_id, ensure?}
    ensure=true 时只加入不移除（幂等，用于关联截图自动加心标）。
    """
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    ensure = bool(data.get("ensure"))
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM wishlist WHERE item_id=?", (item_id,)).fetchone()
    if existing:
        if ensure:
            conn.close()
            return jsonify({"in_wishlist": True})
        conn.execute("DELETE FROM wishlist WHERE item_id=?", (item_id,))
        conn.commit()
        conn.close()
        return jsonify({"in_wishlist": False})
    else:
        conn.execute("INSERT INTO wishlist (item_id) VALUES (?)", (item_id,))
        conn.commit()
        conn.close()
        return jsonify({"in_wishlist": True})


def _write_upload_record(safe_name, coarse, item_id):
    """记录上传（关联部件 + 粗排结果）。失败静默，不影响主流程。"""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO upload_record (file_path, parts_json, item_id) VALUES (?, ?, ?)",
            (safe_name, json.dumps(coarse, ensure_ascii=False),
             int(item_id) if item_id else None),
        )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _write_coarse_log(safe_name, crop_name, types, box, elapsed, coarse):
    """写粗排日志到 recognize.log（追加）。失败静默，不影响主流程。"""
    try:
        top_list = [
            {"item_id": c.get("item_id"), "name": c.get("item_name"), "sim": c.get("sim"),
             "alias": c.get("alias"), "type": c.get("type"), "star": c.get("star"),
             "set_id": c.get("set_id"), "set_name": c.get("set_name"), "obtain": c.get("obtain")}
            for c in (coarse or [])[:10]
        ]
        log_line = {
            "id": _next_log_id(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "coarse",
            "types": types,
            "file": safe_name,
            "crop_file": crop_name,
            "box": box,
            "elapsed": elapsed,
            "top1": top_list[0]["name"] if top_list else None,
            "top": top_list,
        }
        with open(BASE / "recognize.log", "a", encoding="utf-8") as lf:
            lf.write(json.dumps(log_line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _do_recall(crop_file, safe_name, types, box, top, item_id):
    """粗排核心：召回 + 写日志 + 上传记录。独立成函数，供同步调用与后台任务复用。
    异常直接上抛：同步模式由接口转 500，后台模式由 _task_runner 记为 error。"""
    crop_path2 = UPLOAD_DIR / crop_file
    # 同图同部件粗排缓存：同原图+同部位+同框 命中直接返回，跳过 embedding 召回
    ck = None
    cached = False
    coarse = []
    elapsed = 0
    if box:
        orig_path = UPLOAD_DIR / safe_name
        if orig_path.exists():
            ck = _coarse_cache_key(str(orig_path), types, box)
            hit = _cache_get(ck) if ck else None
            if hit is not None and hit.get("coarse"):
                coarse = hit["coarse"]
                elapsed = hit.get("elapsed", 0)
                cached = True
    if not cached:
        from recognize import recall_only
        r = recall_only(str(crop_path2), types=types, top=top)
        coarse = r["coarse"]
        elapsed = r["elapsed"]
        if ck and coarse:
            _cache_set(ck, {"coarse": coarse, "elapsed": elapsed})
    _write_upload_record(safe_name, coarse, item_id)
    _write_coarse_log(safe_name, crop_file, types, box, elapsed, coarse)
    return {
        "file": safe_name,
        "crop_file": crop_file,
        "coarse": coarse,
        "cand_ids": [c["item_id"] for c in coarse if c.get("item_id")],
        "elapsed": elapsed,
        "cached": cached,
    }


def _do_precise(crop_file, cand_ids, types, orig_file):
    """精排核心：查 meta + 精排比对 + 写日志。独立成函数，供同步调用与后台任务复用。
    异常直接上抛：同步模式由接口转 500，后台模式由 _task_runner 记为 error。"""
    crop_path = UPLOAD_DIR / crop_file
    # 组装 (item_id, item_name) 供精排 + 查完整 meta（用于日志回显/加心标/关联）
    conn = get_db()
    names = {}
    meta = {}
    ph = ",".join("?" * len(cand_ids))
    rows = conn.execute(
        "SELECT oi.id, oi.name, oi.type, oi.star, oi.image, oi.image_big, oi.set_id, os.name AS set_name "
        "FROM outfit_item oi LEFT JOIN outfit_set os ON oi.set_id=os.id "
        f"WHERE oi.id IN ({ph})",
        cand_ids,
    ).fetchall()
    for r in rows:
        names[r["id"]] = r["name"]
        meta[r["id"]] = dict(r)
    conn.close()
    cand_pairs = [(cid, names.get(cid, "")) for cid in cand_ids]
    # 发送给模型的候选过多会让识图 API 极慢（服务端要逐张下载远程候选图）。
    # 只送前 8 个最相似的候选，日志/返回仍保留完整候选列表。
    if len(cand_pairs) > 8:
        cand_pairs = cand_pairs[:8]

    from recognize import precise_only
    hit_ids, elapsed = precise_only(str(crop_path), cand_pairs)

    # 写精排日志（追加到 recognize.log）
    try:
        hit_names = [names.get(cid, "") for cid in hit_ids]
        # 候选完整列表（保留粗排顺序）
        top_list = [
            {"item_id": cid, "name": names.get(cid, ""), "type": meta.get(cid, {}).get("type"),
             "star": meta.get(cid, {}).get("star"), "set_id": meta.get(cid, {}).get("set_id"),
             "set_name": meta.get(cid, {}).get("set_name"), "alias": None,
             "obtain": meta.get(cid, {}).get("obtain")}
            for cid in cand_ids
        ]
        log_line = {
            "id": _next_log_id(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "precise",
            "types": types,
            "file": orig_file or crop_file,
            "crop_file": crop_file,
            "elapsed": elapsed,
            "top1": hit_names[0] if hit_names else None,
            "top": top_list,
            "precise_hits": hit_names,
        }
        with open(BASE / "recognize.log", "a", encoding="utf-8") as lf:
            lf.write(json.dumps(log_line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return {"hit_ids": hit_ids, "elapsed": elapsed}


def _do_crop(save_path, safe_name, types, box, crop_path, crop_name):
    """裁剪核心：grounding 出框（带 box 则跳过）+ 裁剪。供同步调用与后台任务复用。
    不写日志、不写 upload_record，可安全重复提交（幂等）。"""
    from recognize import crop_only
    r = crop_only(str(save_path), types=types, zoom=2,
                  save_crop=str(crop_path), box=box)
    return {
        "file": safe_name,
        "crop_file": crop_name,
        "box": r["box"],
        "elapsed": r["elapsed"],
        "cached": r["cached"],
    }


@app.route("/api/recognize", methods=["POST"])
def api_recognize():
    """AI 识别（粗排）。body/form: file, types, item_id(选部件关联), top, use_file(复用已上传原图),
    box(手动框选坐标 x1,y1,x2,y2,可选), step(crop|recall|full, 默认 full), async(1=后台任务)"""
    types = (request.form.get("types") or "连衣裙").strip()  # 空串也回退默认，避免索引名拼接出错
    item_id = request.form.get("item_id", "").strip()
    top = int(request.form.get("top", "10"))
    step = (request.form.get("step") or "full").strip()
    box_raw = (request.form.get("box") or "").strip()
    box = None
    if box_raw:
        nums = re.findall(r"-?\d+", box_raw)
        if len(nums) == 4:
            box = [int(x) for x in nums]
        else:
            return jsonify({"error": "box 参数格式错误，应为 x1,y1,x2,y2"}), 400

    # step=recall：复用已保存裁剪图跑粗排，不需要上传原图（此时才写日志 + upload_record）
    if step == "recall":
        crop_file = (request.form.get("crop_file") or "").strip()
        if not crop_file:
            return jsonify({"error": "缺少 crop_file 参数"}), 400
        crop_path2 = UPLOAD_DIR / crop_file
        if not crop_path2.exists():
            return jsonify({"error": "裁剪图已不存在，请重新裁剪"}), 400
        # 原图文件名优先取 use_file（前端回传），否则退回 crop_file 名
        safe_name = (request.form.get("use_file") or "").strip() or crop_file
        if request.form.get("async") == "1":
            # 后台任务：页面跳转/刷新/关标签都不影响识别，前端轮询 /api/recognize/task/<id>
            task_id = _submit_task(_do_recall, crop_file, safe_name, types, box, top, item_id)
            return jsonify({"task_id": task_id})
        try:
            return jsonify(_do_recall(crop_file, safe_name, types, box, top, item_id))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"粗排失败: {e}"}), 500

    # 复用已上传的原图：一张图查多个部位时不重复上传
    use_file = (request.form.get("use_file") or "").strip()
    if use_file:
        save_path = UPLOAD_DIR / use_file
        if not save_path.exists():
            return jsonify({"error": "缓存的原图已不存在，请重新上传"}), 400
        safe_name = use_file
    else:
        if "file" not in request.files:
            return jsonify({"error": "未收到文件，请重新上传图片"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "文件名为空"}), 400
        ext = Path(f.filename).suffix.lower() or ".png"
        safe_name = f"{int(time.time() * 1000)}{ext}"
        save_path = UPLOAD_DIR / safe_name
        f.save(str(save_path))

    # 裁剪图保存路径（供前端展示）
    crop_name = f"crop_{int(time.time() * 1000)}.png"
    crop_path = UPLOAD_DIR / crop_name

    # step=crop：只出框+裁剪（手动框选带 box 则跳过 grounding），不跑粗排、不写日志
    if step == "crop":
        if request.form.get("async") == "1":
            # 后台任务：裁剪期间页面跳转/刷新也不中断，前端轮询 /api/recognize/task/<id>
            task_id = _submit_task(_do_crop, str(save_path), safe_name, types, box,
                                   str(crop_path), crop_name)
            return jsonify({"task_id": task_id})
        try:
            return jsonify(_do_crop(str(save_path), safe_name, types, box,
                                    str(crop_path), crop_name))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"裁剪失败: {e}", "file": safe_name}), 500

    # step=full（默认）：完整流程，兼容旧调用（首页/日志详情页）
    # 同图同部件粗排缓存：带框调用可命中；不带框首次跑完后用 grounding 框写缓存
    ck = None
    hit = None
    if box:
        ck = _coarse_cache_key(str(save_path), types, box)
        hit = _cache_get(ck) if ck else None
    if hit is not None and hit.get("coarse"):
        # 命中缓存：用缓存框本地裁剪生成 crop 文件（不跑 grounding/召回）
        from recognize import crop_and_zoom
        try:
            crop_img = crop_and_zoom(str(save_path), hit.get("box") or box, zoom=2)
            crop_img.save(str(crop_path))
        except Exception:  # noqa: BLE001
            crop_path = None
            crop_name = None
        result = {
            "box": hit.get("box") or box,
            "crop_path": str(crop_path) if crop_path else None,
            "coarse": hit["coarse"],
            "elapsed": hit.get("elapsed", 0),
            "cached": True,
        }
    else:
        try:
            from recognize import recognize
            result = recognize(str(save_path), types=types,
                               top=top, zoom=2, save_crop=str(crop_path), box=box)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"识别失败: {e}", "file": safe_name}), 500
        if result.get("box"):
            wck = _coarse_cache_key(str(save_path), types, result["box"])
            if wck and result.get("coarse"):
                _cache_set(wck, {"coarse": result["coarse"], "elapsed": result["elapsed"]})

    _write_upload_record(safe_name, result.get("coarse", []), item_id)
    _write_coarse_log(safe_name, crop_name, types, result.get("box"),
                      result.get("elapsed"), result.get("coarse", []))

    return jsonify({
        "file": safe_name,
        "crop_file": crop_name,
        "box": result.get("box"),
        "coarse": result.get("coarse", []),
        "cand_ids": [c["item_id"] for c in result.get("coarse", []) if c.get("item_id")],
        "elapsed": result.get("elapsed"),
        "cached": result.get("cached", False),
    })


# ---- iOS 快捷指令异步识别 ----
# 一次完整识别约 60~70s（qwen 粗排），超过快捷指令默认 60s 超时，
# 故提供 async 模式：提交后立即返回 task_id，手机端轮询结果接口。
MOBILE_TASKS = {}
MOBILE_TASKS_LOCK = threading.Lock()
MOBILE_TASK_TTL = 1800  # 任务结果保留 30 分钟


def _mobile_recognize_worker(task_id, save_path, crop_path, types, top):
    """后台线程执行识别，结果写入 MOBILE_TASKS。"""
    try:
        from recognize import recognize
        result = recognize(str(save_path), types=types, top=top, zoom=2, save_crop=str(crop_path))
        coarse = result.get("coarse", [])
        top_list = []
        for c in coarse[:top]:
            sim = c.get("sim")
            top_list.append({
                "name": c.get("item_name"),
                "type": c.get("type"),
                "star": c.get("star"),
                "sim": f"{sim * 100:.1f}%" if isinstance(sim, (int, float)) else "",
                "set_name": c.get("set_name"),
            })
        _write_coarse_log(Path(save_path).name, Path(crop_path).name, types,
                          result.get("box"), result.get("elapsed"), coarse)
        with MOBILE_TASKS_LOCK:
            MOBILE_TASKS[task_id] = {
                "status": "done",
                "top": top_list,
                "box": result.get("box"),
                "elapsed": result.get("elapsed"),
                "cached": result.get("cached", False),
                "ts": time.time(),
            }
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "cannot identify image file" in msg or "UnidentifiedImageError" in msg:
            msg = "图片格式不支持（可能是 HEIC），请在快捷指令中先「转换图像」为 JPEG"
        with MOBILE_TASKS_LOCK:
            MOBILE_TASKS[task_id] = {"status": "error", "error": f"识别失败: {msg}", "ts": time.time()}
    finally:
        # 手机上传的原图用后即删；crop 中间产物保留供复盘
        try:
            Path(save_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


@app.route("/api/recognize/mobile", methods=["POST"])
def api_recognize_mobile():
    """iOS 快捷指令专用接口：multipart 上传 file + URL 查询参数 types/top/async。
    快捷指令的 multipart 请求体无法附加表单字段，故部位(types)/数量(top)走 URL 查询参数，
    图片作为 multipart 文件字段 file。async=1 时立即返回 task_id，轮询 /api/recognize/mobile/result。"""
    types = (request.args.get("types") or "连衣裙").strip()
    try:
        top = max(1, min(int(request.args.get("top", "5")), 20))
    except ValueError:
        top = 5
    if "file" not in request.files:
        return jsonify({"error": "未收到文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    ext = Path(f.filename).suffix.lower() or ".png"
    safe_name = f"mobile_{int(time.time() * 1000)}{ext}"
    save_path = UPLOAD_DIR / safe_name
    f.save(str(save_path))
    crop_name = f"crop_{int(time.time() * 1000)}.png"
    crop_path = UPLOAD_DIR / crop_name
    if request.args.get("async") in ("1", "true", "yes"):
        # 异步模式：后台线程识别，立即返回 task_id，手机端轮询结果
        task_id = uuid.uuid4().hex[:12]
        with MOBILE_TASKS_LOCK:
            MOBILE_TASKS[task_id] = {"status": "pending", "ts": time.time()}
        threading.Thread(
            target=_mobile_recognize_worker,
            args=(task_id, save_path, crop_path, types, top),
            daemon=True,
        ).start()
        return jsonify({"task_id": task_id})
    # 同步模式：直接返回结果（约 60~70s，仅调试用）
    try:
        from recognize import recognize
        result = recognize(str(save_path), types=types, top=top, zoom=2, save_crop=str(crop_path))
    except Exception as e:  # noqa: BLE001
        # 常见原因：iPhone 默认 HEIC，Pillow 打不开
        msg = str(e)
        if "cannot identify image file" in msg or "UnidentifiedImageError" in msg:
            msg = "图片格式不支持（可能是 HEIC），请在快捷指令中先「转换图像」为 JPEG"
        return jsonify({"error": f"识别失败: {msg}"}), 500
    coarse = result.get("coarse", [])
    top_list = []
    for c in coarse[:top]:
        sim = c.get("sim")
        top_list.append({
            "name": c.get("item_name"),
            "type": c.get("type"),
            "star": c.get("star"),
            "sim": f"{sim * 100:.1f}%" if isinstance(sim, (int, float)) else "",
            "set_name": c.get("set_name"),
        })
    _write_coarse_log(safe_name, crop_name, types, result.get("box"),
                      result.get("elapsed"), coarse)
    return jsonify({
        "top": top_list,
        "box": result.get("box"),
        "elapsed": result.get("elapsed"),
        "cached": result.get("cached", False),
    })


@app.route("/api/recognize/mobile/result")
def api_recognize_mobile_result():
    """异步任务结果查询：status=pending|done|error。done 时带 top/box/elapsed/cached。"""
    task_id = request.args.get("task_id", "")
    now = time.time()
    with MOBILE_TASKS_LOCK:
        t = MOBILE_TASKS.get(task_id)
        # 顺手清理过期任务，防止内存膨胀
        for k in [k for k, v in MOBILE_TASKS.items() if now - v["ts"] > MOBILE_TASK_TTL]:
            MOBILE_TASKS.pop(k, None)
    if not t:
        return jsonify({"error": "任务不存在或已过期"}), 404
    if t["status"] == "error":
        return jsonify({"status": "error", "error": t["error"]}), 500
    if t["status"] == "done":
        return jsonify({"status": "done", "top": t["top"], "box": t.get("box"),
                        "elapsed": t.get("elapsed"), "cached": t.get("cached", False)})
    return jsonify({"status": "pending"})


@app.route("/api/recognize/precise", methods=["POST"])
def api_recognize_precise():
    """精排：复用粗排已生成的裁剪图 + 候选列表，不重跑粗排。
    body/form: crop_file(裁剪图文件名), cand_ids(逗号分隔的 item_id 列表), async(1=后台任务)
    """
    crop_file = (request.form.get("crop_file") or "").strip()
    cand_raw = (request.form.get("cand_ids") or "").strip()
    types = (request.form.get("types") or "精排").strip()
    orig_file = (request.form.get("file") or "").strip()   # 上传原图文件名（用于关联截图）
    if not crop_file:
        return jsonify({"error": "缺少 crop_file"}), 400
    crop_path = UPLOAD_DIR / crop_file
    if not crop_path.exists():
        return jsonify({"error": "裁剪图不存在，请先跑粗排"}), 400
    if not cand_raw:
        return jsonify({"error": "缺少 cand_ids"}), 400
    try:
        cand_ids = [int(x) for x in cand_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "cand_ids 格式错误"}), 400
    if not cand_ids:
        return jsonify({"error": "候选列表为空"}), 400

    if request.form.get("async") == "1":
        # 后台任务：页面跳转/刷新不影响精排，前端轮询 /api/recognize/task/<id>
        task_id = _submit_task(_do_precise, crop_file, cand_ids, types, orig_file)
        return jsonify({"task_id": task_id})
    try:
        return jsonify(_do_precise(crop_file, cand_ids, types, orig_file))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"精排失败: {e}"}), 500


@app.route("/api/recognize/task/<task_id>", methods=["GET"])
def api_task_status(task_id):
    """查询后台识别任务状态：running / done / error。"""
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
    if t is None:
        return jsonify({"error": "任务不存在或已过期"}), 404
    return jsonify({"status": t["status"], "result": t["result"], "error": t["error"]})


@app.route("/api/recognize/task/<task_id>/cancel", methods=["POST"])
def api_task_cancel(task_id):
    """请求取消后台任务（协作式：不强制杀线程；recall_only 本身不可打断，取消只影响前端轮询）。"""
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if t is not None:
            t["cancel"] = True
    return jsonify({"ok": True})


def _next_log_id():
    """识别日志自增 id：持久化到 recognize_log.seq，跨进程单调递增。旧日志无 id，不处理。"""
    # 后台任务线程与请求线程可能并发取号，加进程内锁保证单调
    with _SEQ_LOCK:
        seq_file = BASE / "recognize_log.seq"
        try:
            cur = int((seq_file.read_text(encoding="utf-8").strip() or "0"))
        except Exception:  # noqa: BLE001
            cur = 0
        nid = cur + 1
        try:
            seq_file.write_text(str(nid), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return nid


# ---------- 后台识别任务管理 ----------
# 识别长任务（粗排/精排）与页面生命周期解耦：提交任务立即返回 task_id，
# 页面刷新/跳转不影响后台线程继续跑，前端轮询 /api/recognize/task/<id> 取结果。
_TASKS = {}
_TASKS_LOCK = threading.Lock()
_SEQ_LOCK = threading.Lock()
_TASK_MAX_AGE = 1800  # 完成/失败任务保留 30 分钟，下次提交时顺带清理


def _submit_task(fn, *args, **kwargs):
    """提交后台任务，立即返回 task_id。fn 在 daemon 线程中执行，异常自动捕获为 error 状态。"""
    _cleanup_tasks()
    task_id = uuid.uuid4().hex[:12]
    t = {"id": task_id, "status": "running", "result": None, "error": None,
         "cancel": False, "time": time.time()}
    with _TASKS_LOCK:
        _TASKS[task_id] = t
    threading.Thread(target=_task_runner, args=(task_id, fn, args, kwargs), daemon=True).start()
    return task_id


def _task_runner(task_id, fn, args, kwargs):
    try:
        result = fn(*args, **kwargs)
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is not None:
                t["status"] = "done"
                t["result"] = result
    except Exception as e:  # noqa: BLE001
        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t is not None:
                t["status"] = "error"
                t["error"] = str(e)


def _cleanup_tasks():
    """清理已结束且超时的任务，防止内存泄漏。"""
    now = time.time()
    with _TASKS_LOCK:
        for tid in [k for k, t in _TASKS.items()
                    if t["status"] != "running" and now - t["time"] > _TASK_MAX_AGE]:
            _TASKS.pop(tid, None)


@app.route("/api/recognize/log", methods=["GET"])
def api_recognize_log():
    """查看识别日志（最近 N 条，默认100，上限1000）。query: n"""
    n = min(int(request.args.get("n", "100")), 1000)
    log_file = BASE / "recognize.log"
    if not log_file.exists():
        return jsonify({"logs": [], "total": 0})
    # 行数用二进制快速扫描（不做解码/JSON 解析）；只解析文件尾部，
    # 避免日志膨胀后每次轮询全量读取+全量解析导致接口卡顿
    with open(log_file, "rb") as f:
        data = f.read()
    total = data.count(b"\n")  # 日志总条数（供前端统计展示）
    tail = data[-2_000_000:].decode("utf-8", errors="replace")  # 尾部 2MB 足够容纳 n 条
    logs = []
    for line in tail.splitlines()[-n:]:
        try:
            logs.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    logs.reverse()
    return jsonify({"logs": logs, "total": total})


@app.route("/api/recognize/log/<int:lid>", methods=["GET"])
def api_recognize_log_one(lid):
    """按自增 id 取单条识别日志（旧日志无 id 返回 404）"""
    log_file = BASE / "recognize.log"
    if not log_file.exists():
        return jsonify({"error": "日志不存在"}), 404
    for line in log_file.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("id") == lid:
            return jsonify({"log": d})
    return jsonify({"error": "日志不存在"}), 404


@app.route("/recognize/log/<int:lid>")
def recognize_log_page(lid):
    """日志详情独立页：渲染同一识别模板，前端按 URL 定位渲染对应日志"""
    conn = get_db()
    present = {r[0] for r in conn.execute("SELECT DISTINCT type FROM outfit_item WHERE type IS NOT NULL AND type != ''").fetchall()}
    conn.close()
    types = [t for t in TYPE_ORDER if t in present] + sorted(present - set(TYPE_ORDER))
    return render_template("recognize.html", types=types)


@app.route("/api/photo/link", methods=["POST"])
def api_photo_link():
    """识别结果勾选确认后，把照片关联到部件。body: {photo_id 或 file, item_id}"""
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400

    conn = get_db()
    # 优先用 photo_id（已有记录），否则用 file 找最新记录
    photo_id = data.get("photo_id")
    file_name = data.get("file")
    if photo_id:
        conn.execute("UPDATE upload_record SET item_id=? WHERE id=?", (item_id, photo_id))
    elif file_name:
        conn.execute(
            "UPDATE upload_record SET item_id=? WHERE file_path=? AND id=(SELECT MAX(id) FROM upload_record WHERE file_path=?)",
            (item_id, file_name, file_name),
        )
    else:
        conn.close()
        return jsonify({"error": "缺少 photo_id 或 file"}), 400
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/parts/search", methods=["GET"])
def api_parts_search():
    """按部件名 / 套装别名模糊搜索（手动传图时选部件用）。query: q
    排序：部件名精确 > 部件名前缀 > 别名精确 > 别名模糊 > 其余；最多 20 条。"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"items": []})
    conn = get_db()
    like = f"%{q}%"
    rows = conn.execute(
        "SELECT i.id, i.name, i.type, i.star, i.image, i.image_big, i.set_id, "
        "s.name AS set_name, s.alias AS set_alias "
        "FROM outfit_item i LEFT JOIN outfit_set s ON i.set_id = s.id "
        "WHERE i.name LIKE ? OR s.alias LIKE ? "
        "ORDER BY (i.name = ?) DESC, (i.name LIKE ?) DESC, "
        "(s.alias = ?) DESC, (s.alias LIKE ?) DESC, i.id LIMIT 20",
        (like, like, q, f"{q}%", q, like),
    ).fetchall()
    conn.close()
    return jsonify({"items": [dict(r) for r in rows]})


@app.route("/api/photo/upload", methods=["POST"])
def api_photo_upload():
    """手动上传参考图并直接关联到指定部件（item_id）。
    form: file(图片), item_id(部件ID)。不跑识别，仅保存文件 + 写入 upload_record 关联。
    """
    if "file" not in request.files:
        return jsonify({"error": "未收到文件"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    item_id = (request.form.get("item_id") or "").strip()
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400
    try:
        item_id = int(item_id)
    except ValueError:
        return jsonify({"error": "item_id 格式错误"}), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return jsonify({"error": "仅支持图片 (jpg/png/webp/bmp)"}), 400

    safe_name = f"ref_{int(time.time() * 1000)}{suffix}"
    save_path = UPLOAD_DIR / safe_name
    f.save(str(save_path))

    conn = get_db()
    # 确认部件存在
    row = conn.execute("SELECT id FROM outfit_item WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        try:
            save_path.unlink()
        except OSError:
            pass
        return jsonify({"error": "部件不存在"}), 404
    conn.execute(
        "INSERT INTO upload_record (file_path, parts_json, item_id) VALUES (?, ?, ?)",
        (safe_name, json.dumps([], ensure_ascii=False), item_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "file_path": safe_name})


@app.route("/api/part/image", methods=["GET"])
def api_part_image():
    """按 item_id 查当前部件大图（详情回显用，避免日志存长路径）。query: item_id"""
    item_id = request.args.get("item_id")
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400
    conn = get_db()
    row = conn.execute(
        "SELECT id, name, type, star, obtain, image, image_big, set_id FROM outfit_item WHERE id=?",
        (item_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"image_big": "", "image": ""})
    return jsonify({
        "item_id": row["id"], "name": row["name"] or "", "type": row["type"] or "",
        "star": row["star"], "obtain": row["obtain"] or "", "set_id": row["set_id"],
        "image_big": row["image_big"] or "", "image": row["image"] or "",
    })


@app.route("/api/part/forms/<int:item_id>")
def api_part_forms(item_id):
    """按 item_id 查部件多形态（outfit_item_forms）。无多形态返回空数组。

    供 wishlist/识别等页面在需要时单独拉取；/api/sets 与 /api/set/<id>
    已内联各部件 forms 数组，主流程无需再调此接口。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT form_no, image, image_big FROM outfit_item_forms WHERE item_id=? ORDER BY form_no",
        (item_id,),
    ).fetchall()
    conn.close()
    return jsonify({"forms": [dict(r) for r in rows]})


@app.route("/api/parts-photos", methods=["GET"])
def api_parts_photos():
    """查询某部件的 AI 识别照片。query: item_id"""
    item_id = request.args.get("item_id")
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT id, file_path, parts_json, created_at FROM upload_record "
        "WHERE item_id=? ORDER BY id DESC", (item_id,)
    ).fetchall()
    conn.close()
    return jsonify({"photos": [dict(r) for r in rows]})


@app.route("/api/photo/delete", methods=["POST"])
def api_photo_delete():
    """删除某条照片记录。body: {id}"""
    data = request.get_json(silent=True) or {}
    pid = data.get("id")
    if not pid:
        return jsonify({"error": "缺少 id"}), 400

    conn = get_db()
    row = conn.execute("SELECT file_path FROM upload_record WHERE id=?", (pid,)).fetchone()
    if row:
        # 删除磁盘文件（若存在）
        p = UPLOAD_DIR / row["file_path"]
        if p.exists():
            p.unlink()
        conn.execute("DELETE FROM upload_record WHERE id=?", (pid,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/photo/delete-all", methods=["POST"])
def api_photo_delete_all():
    """一键删除某部件的全部内容：关联的参考图 + 从心愿单移除。
    body: {item_id}
    """
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400

    conn = get_db()
    # 删除该部件全部上传参考图（磁盘 + 记录）
    rows = conn.execute(
        "SELECT file_path FROM upload_record WHERE item_id=?", (item_id,)
    ).fetchall()
    for r in rows:
        p = UPLOAD_DIR / r["file_path"]
        if p.exists():
            p.unlink()
    conn.execute("DELETE FROM upload_record WHERE item_id=?", (item_id,))
    # 从心愿单移除该部件
    conn.execute("DELETE FROM wishlist WHERE item_id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": len(rows)})


if __name__ == "__main__":
    cleanup_stale_uploads()  # 启动时清理过期临时上传文件
    # 监听 0.0.0.0：允许局域网内 iOS 快捷指令访问（本机访问不受影响）
    app.run(host="0.0.0.0", port=5050, debug=False)
