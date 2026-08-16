"""
Grok 账号自动补位系统 v2
───────────────────────
完整流水线: Clash IP轮换 → 邮箱注册(SSO) → PKCE转换(CPA) → Token刷新
监控 auths/ 目录，可用账号 < 阈值时自动补位。

用法:
  python auto_replenish.py                          # 一次性检查补位
  python auto_replenish.py --daemon 600             # 每 10 分钟守护
  python auto_replenish.py --min 3 --rotate-region  # 保持 3 个，跨区域切换
  python auto_replenish.py --check                  # 仅检查状态
"""
import os, sys, time, json, subprocess, argparse, random
from datetime import datetime, timezone
import requests as _requests
import urllib3
urllib3.disable_warnings()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _utc_to_ts(utc_str):
    """解析 UTC ISO 时间戳 → Unix timestamp"""
    try:
        return datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None

import logging
from config import AUTH_DIR, KEYS_DIR, LOG_DIR, SCRIPT_DIR, setup_logging

from config import PROXY  # noqa: E402(代理单一来源,原 4 行重复解析删除)（美国 VPS）
GROK2API_BASE = os.getenv("GROK2API_BASE") or "http://127.0.0.1:8000"
GROK2API_USER = os.getenv("GROK2API_USER") or "admin"
GROK2API_PASS = os.getenv("GROK2API_PASS") or ""
# 单轮补位注册上限（.env 可覆盖；防止账号池暴跌时一轮注册过多）
REGISTER_CAP = int(os.getenv("GROK_REGISTER_CAP", "5"))
# 单号注册重试上限（.env GROK_REG_MAX_RETRIES 可覆盖）。注意:每次重试 = 重跑 grok.py
# = 新买一个邮箱(LuckMail 无退回 API)+ 新验证码,默认 3 把单号最坏开销压在 3 邮箱 + 9 验证码
REG_MAX_RETRIES = int(os.getenv("GROK_REG_MAX_RETRIES", "3"))
# ── 免费路径节奏参数(2026-08-16 用户决策:水位 100,更省成本,全部 .env 可调) ──
# 免费路径失败重试(默认关:抑制/限流期重试=白烧邮箱 0.02,换 IP 通常救不回来)
FREE_RETRY = os.getenv("GROK_FREE_RETRY", "0").strip().lower() in ("1", "true", "yes", "on")
# 免费路径连续失败提前终止阈值(默认 2;设 1 最省——抑制期 1 号失败即全灭)
FREE_FAIL_STREAK = int(os.getenv("GROK_FREE_FAIL_STREAK", "2"))
# 免费路径零增长后的休眠秒数(默认 3600=1h;设 86400 对齐次日抑制窗口)
FREE_SUPPRESSION_SLEEP = int(os.getenv("GROK_FREE_SUPPRESSION_SLEEP", "3600"))
# ── 二级长熔断(2026-08-16 用户需求:连续 N 轮零增长 → 熔断到次日 + TG 推送) ──
# 默认 0=禁用(保守);VPS 显式设 3(连续 3 轮全败即长熔断)
FREE_CIRCUIT_STREAK = int(os.getenv("GROK_FREE_CIRCUIT_STREAK", "0"))
# 长熔断休眠秒数(默认 86400 = 次日窗口)
FREE_CIRCUIT_SLEEP = int(os.getenv("GROK_FREE_CIRCUIT_SLEEP", "86400"))


def _grok2api_db() -> str:
    """grok2api SQLite 路径统一解析。

    优先级:GROK2API_DB env → 实际存在的候选路径(项目同级 grok2api/ 或
    ~/.grok-register/) → 返回 ~/.grok-register 兜底。
    注意:不再有 Windows 兜底——D:\\grok2api\\... 在 Linux 上必然不存在,
    曾导致 Web 池计数恒 0 → 无限注册烧 YesCaptcha(README FAQ)。
    """
    db = os.getenv("GROK2API_DB", "").strip()
    if db:
        return db
    for cand in (
        os.path.join(os.path.dirname(SCRIPT_DIR), "grok2api", "data", "backend.db"),
        os.path.join(os.path.expanduser("~"), ".grok-register", "data", "backend.db"),
    ):
        if os.path.exists(cand):
            return cand
    return os.path.join(os.path.expanduser("~"), ".grok-register", "data", "backend.db")
GROK_REG = os.path.join(SCRIPT_DIR, "grok.py")          # YesCaptcha 版（高成功率）
GROK_FREE = os.path.join(SCRIPT_DIR, "grok_free.py")    # DrissionPage 免费版（备用）
# 刷新职责单一归属 grok2api(自治 + refresh-tokens API);本地 token_daemon 已移除
# (2026-08-16),防止双刷互相作废 refresh_token。
# 直接导入转换函数，不走子进程
# 2026-08-15: PKCE (sso_to_cpa) 已被 CF 拦截且 consent 无法处理,整个模块已删除;
# 统一走 Device Flow (device_mint)——纯浏览器转换,不花钱。
from auth_store import save_auth
try:
    sys.path.insert(0, SCRIPT_DIR)
    from device_mint import sso_to_device as _sso_to_device_direct
    CONVERT_MODE = "device"
    HAS_DIRECT_CONVERT = True
except ImportError:
    HAS_DIRECT_CONVERT = False

# Clash 轮换器（IP 轮换防风控）
# clash_available() 按 API 可达性判定，而非 import 成功与否：
# clash_rotator.py 在仓库内，VPS/无 Clash 机器上 import 必然成功，
# 但轮换 API 不存在 → 必须探测后才能决定是否启用轮换。
# 2026-08-16:改为惰性探测——import 不发网络请求(opt-notes 补),首次使用才 health()
_clash_ok = None


def clash_available() -> bool:
    """惰性判定 Clash API 可达性(只探测一次,import 零网络请求)。"""
    global _clash_ok
    if _clash_ok is None:
        try:
            _clash_ok = bool(clash_health().get("ok"))
            if not _clash_ok:
                print("[IP] Clash API 不可达 → IP 轮换已禁用（固定出口直连）")
        except Exception:
            _clash_ok = False
            print("[IP] Clash API 不可达 → IP 轮换已禁用（固定出口直连）")
    return _clash_ok


try:
    from clash_rotator import (random_switch, switch_region, get_current_ip,
                                health as clash_health, snapshot, restore)
except ImportError:
    pass


# ── IP 洁净度探测 ──
CF_MARKERS = [b"Cloudflare", b"Attention Required", b"cf-challenge",
              b"cf-browser-verification", b"Just a moment"]
# grok.py 实际请求 accounts.x.ai，不是 x.com
PROBE_URLS = ["https://accounts.x.ai/sign-up", "https://x.com"]
PROBE_TIMEOUT = 15


def probe_ip(ip=None):
    """通过 Clash 代理探测 accounts.x.ai（grok.py 的真正目标）。"""
    proxies = {"http": PROXY, "https": PROXY} if (clash_available() and PROXY) else None
    for url in PROBE_URLS:
        try:
            resp = _requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                proxies=proxies, timeout=PROBE_TIMEOUT,
                                verify=False)
            body = resp.content[:8192]
            for marker in CF_MARKERS:
                if marker in body:
                    return False, f"CF blocked {url} (marker={marker.decode()[:30]})"
        except Exception as e:
            return False, f"{url}: {type(e).__name__}"
    return True, "all clean"


def find_clean_ip(max_attempts=10):
    """轮换 Clash 节点直到找到洁净 IP，返回 IP 或 None。"""
    if not clash_available():
        return None
    for i in range(max_attempts):
        try:
            random_switch()
        except Exception as e:
            print(f"  [IP] switch failed: {e}")
        time.sleep(2)
        ip = get_current_ip()
        clean, detail = probe_ip()
        status = "✅" if clean else "❌"
        print(f"  [IP] #{i+1} {ip} {status} - {detail}")
        if clean:
            return ip
        time.sleep(1)
    return None


# ── 单号注册（带 IP 重试）──
GROK_TXT = os.path.join(KEYS_DIR, "grok.txt")
ACCOUNTS_TXT = os.path.join(KEYS_DIR, "accounts.txt")


def _file_lines(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return []


def register_one(script, extra_args, max_retries=5):
    """注册 1 个账号，失败换 IP 重试。返回 (ok: bool, sso_token: str|None, email: str|None)。
    注意：不使用 probe_ip()（requests 库会触发 CF 拦截），
    直接跑 grok.py（curl_cffi 可绕过 CF），从 stdout 判断是否被拦截。"""
    for attempt in range(1, max_retries + 1):
        if attempt > 1 and clash_available():
            print(f"  [RETRY] 换 IP 重试 ({attempt}/{max_retries})...")
            find_clean_ip(max_attempts=5)
        else:
            ip = get_current_ip()
            print(f"  [IP] {ip} — 直接运行 grok.py（跳过探测，避免触发 CF 拦截）")

        # 记录注册前 grok.txt 行数
        before_lines = _file_lines(GROK_TXT)
        before_accts = _file_lines(ACCOUNTS_TXT)

        cmd = [sys.executable, script] + extra_args + ["--count", "1"]
        try:
            result = subprocess.run(
                cmd, cwd=SCRIPT_DIR,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=180
            )
        except subprocess.TimeoutExpired:
            print(f"  [TIMEOUT] 注册超时")
            continue
        except Exception as e:
            print(f"  [ERR] {e}")
            continue

        # 检查是否被 CF 拦截（grok.py 返回码 0 但实际被拦截）
        if "Blocked due to abusive traffic patterns" in result.stdout or "Blocked due to abusive traffic patterns" in result.stderr:
            print(f"  [CF] ❌ 被 Cloudflare 拦截，换 IP 重试")
            continue

        # 从 stdout 提取 SSO（优先，因为文件写入可能因 CWD 问题失败）
        import re as _re
        sso_found = None
        email_found = None
        for line in result.stdout.splitlines():
            m = _re.search(r'注册成功:\s*(\S+)\s*\|\s*SSO:\s*(\S+)', line)
            if m:
                email_found = m.group(1)
                sso_found = m.group(2)
                break

        if sso_found and email_found:
            print(f"  [OK] {email_found} SSO={sso_found[:15]}... (stdout)")
            return True, sso_found, email_found

        # 兜底：检查文件是否写入
        after_lines = _file_lines(GROK_TXT)
        after_accts = _file_lines(ACCOUNTS_TXT)
        new_sso = [l for l in after_lines if l not in before_lines]
        new_accts = [l for l in after_accts if l not in before_accts]
        if new_accts:
            for line in new_accts:
                parts = line.split(":")
                if len(parts) >= 3:
                    email = parts[0].replace("﻿", "")
                    sso = parts[2]
                    print(f"  [OK] {email} SSO={sso[:15]}... (file)")
                    return True, sso, email

        # 打印输出以便排查
        if result.stdout.strip():
            preview = result.stdout.strip()[:500]
            # 只打印非空结果
            if "注册成功" not in preview and "Action ID" not in preview:
                print(f"  [STDOUT] {preview}")
        if result.stderr.strip():
            print(f"  [STDERR] {result.stderr.strip()[:300]}")

        print(f"  [FAIL] 注册未产生 SSO（返回码 {result.returncode}）")
    return False, None, None


def count_available():
    """统计 grok2api Build 池可用账号数(provider='grok_build' AND auth_status='active')。

    2026-08-15 改:原来数本地 auths/*.json —— 本地存档缺失(如误删)会让计数失真,
    阈值按失真数触发无谓注册(烧钱)。grok2api DB 才是账号池的权威数据源。
    """
    import sqlite3 as _sq
    db = _grok2api_db()
    if not os.path.exists(db):
        print(f"  [BUILD-COUNT] ⚠️ 数据库不存在: {db!r}")
        print(f"  [BUILD-COUNT] ⚠️ 设 GROK2API_DB 指向 grok2api 的 backend.db,否则 Build 计数为 0")
        return 0, []
    try:
        conn = _sq.connect(db)
        cur = conn.cursor()
        cur.execute(
            "SELECT p.email FROM provider_accounts p "
            "WHERE p.provider = 'grok_build' AND p.auth_status = 'active'")
        emails = [r[0] for r in cur.fetchall()]
        conn.close()
        return len(emails), emails
    except Exception as e:
        print(f"  [BUILD-COUNT] {e}")
        return 0, []


def run_registration(count=3, rotate_region=False, use_free=False):
    """逐个注册账号，每个号失败自动换 IP 重试。返回 (success_count, sso_list)。"""
    script = GROK_FREE if use_free else GROK_REG
    script_name = "grok_free.py (DrissionPage)" if use_free else "grok.py (YesCaptcha)"
    print(f"\n{'='*50}")
    print(f"[STEP 1/3] 逐个注册 {count} 个账号 — {script_name}")
    print(f"{'='*50}")

    if use_free:
        # DrissionPage 浏览器模式：自带 CF 绕过能力，不需要 IP 探测
        extra_args = ["--no-rotate", "--count", "1", "--min-delay", "8", "--max-delay", "15"]
        timeout = 300  # 浏览器单个号 5 分钟
    else:
        extra_args = ["--threads", "1", "--email-provider", os.getenv("EMAIL_PROVIDER", "luckmail_order")]
        timeout = 180

    success = 0
    sso_list = []
    fail_streak = 0
    for idx in range(count):
        tag = f"[{idx+1}/{count}]"
        print(f"\n{tag} 开始注册第 {idx+1} 个...")
        if use_free:
            # 每号轮换 IP(2026-08-16 实测:x.ai 对单 IP 发码限流 ~3-5 次/小时,
            # 换节点即恢复。浏览器单号子进程 --no-rotate,轮换必须在这里做)
            if clash_available():
                try:
                    from clash_rotator import random_switch
                    random_switch()
                    time.sleep(2)
                except Exception as e:
                    print(f"  [IP] ⚠️ 轮换失败: {e}")
            ok, sso, email = register_one_free(script, extra_args, timeout)
            # 失败重试(默认关,GROK_FREE_RETRY=1 启用;每次重试=新买邮箱 0.02)
            if not ok and FREE_RETRY:
                if clash_available():
                    try:
                        from clash_rotator import random_switch
                        random_switch()
                        time.sleep(2)
                    except Exception as e:
                        print(f"  [IP] ⚠️ 重试轮换失败: {e}")
                ok, sso, email = register_one_free(script, extra_args, timeout)
        else:
            ok, sso, email = register_one(script, extra_args, max_retries=REG_MAX_RETRIES)
        if ok:
            success += 1
            if sso and email:
                sso_list.append({"sso": sso, "email": email})
            print(f"{tag} ✅ ({success}/{count} 成功)")
            fail_streak = 0
            time.sleep(3)
        else:
            print(f"{tag} ❌ 注册失败，跳过")
            fail_streak += 1
            # 系统性抑制检测(2026-08-16):x.ai 发码预算耗尽时所有节点都无码,
            # 连续 N 号失败(FREE_FAIL_STREAK)说明在抑制窗口内 → 提前终止本轮
            if use_free and fail_streak >= FREE_FAIL_STREAK and idx < count - 1:
                print(f"{tag} ⚠️ 连续 {fail_streak} 号无码失败,疑似 x.ai 系统性发码抑制,"
                      f"提前终止本轮(剩余 {count - idx - 1} 个跳过)")
                break

    print(f"\n注册结束: {success}/{count} 成功")
    return success, sso_list


def register_one_free(script, extra_args, timeout=300):
    """浏览器模式注册单个号。DrissionPage 自带 CF 绕过，不额外探测 IP。
    返回 (ok, sso, email)。"""
    before_lines = _file_lines(GROK_TXT)
    before_accts = _file_lines(ACCOUNTS_TXT)

    cmd = [sys.executable, script] + extra_args
    ip = get_current_ip() if clash_available() else "unknown"
    print(f"  [IP] {ip} (browser mode, skip probe)")

    try:
        result = subprocess.run(
            cmd, cwd=SCRIPT_DIR,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout
        )
        # 子进程输出落盘(2026-08-16:原被丢弃,失败环节无法分析)
        try:
            with open(os.path.join(LOG_DIR, "grok_free_sub.log"), "a", encoding="utf-8") as f:
                f.write(f"\n{'='*20} {time.strftime('%Y-%m-%d %H:%M:%S')} ip={ip} rc={result.returncode} {'='*20}\n")
                f.write((result.stdout or "")[-2000:])
                f.write((result.stderr or "")[-2000:])
        except OSError:
            pass
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] 注册超时")
        return False, None, None
    except Exception as e:
        print(f"  [ERR] {e}")
        return False, None, None

    after_lines = _file_lines(GROK_TXT)
    after_accts = _file_lines(ACCOUNTS_TXT)
    new_sso = [l for l in after_lines if l not in before_lines]
    new_accts = [l for l in after_accts if l not in before_accts]

    if new_sso and new_accts:
        for line in new_accts[-len(new_sso):]:
            parts = line.split(":")
            if len(parts) >= 3:
                email = parts[0].replace("﻿", "")
                sso = parts[2]
                print(f"  [OK] {email} SSO={sso[:15]}...")
                return True, sso, email

    if result.returncode == 0:
        # 注册成功但 diff 未捕获 → 取 accounts.txt 最后一行(子进程刚写入)
        try:
            last_line = _file_lines(ACCOUNTS_TXT)[-1]
            parts = last_line.split(":")
            if len(parts) >= 3:
                email = parts[0].replace("\ufeff", "").strip()
                sso = parts[2].strip()
                if email and sso and len(sso) > 20:
                    print(f"  [OK] {email} SSO={sso[:15]}... (returncode 0 fallback)")
                    return True, sso, email
        except Exception as e:
            print(f"  [WARN] returncode 0 但 SSO 提取失败: {e}")
        print(f"  [OK] 注册进程返回 0")
        return True, None, None

    print(f"  [FAIL] 注册返回 {result.returncode}")
    return False, None, None


def convert_sso_list(sso_list):
    """Device Flow 铸造(device_mint)SSO → OAuth token,返回成功数。纯浏览器,不花钱。"""
    if not HAS_DIRECT_CONVERT:
        return 0
    print(f"\n{'='*50}")
    print(f"[STEP 2/3] 转换 {len(sso_list)} 个 SSO → CPA...")
    print(f"{'='*50}")
    success = 0
    for item in sso_list:
        email = item["email"]
        sso = item["sso"]
        print(f"\n转换: {email}")
        for attempt in range(1, 4):
            if attempt > 1:
                print(f"  [RETRY] 换 IP 重试 ({attempt}/3)...")
                find_clean_ip(max_attempts=5)
            result = _sso_to_device_direct(sso, email)
            if result:
                save_auth(email, result)
                success += 1
                break
            else:
                print(f"  [FAIL] 尝试 {attempt}/3 失败")
            time.sleep(2)
    print(f"\n转换结束: {success}/{len(sso_list)} 成功")
    return success


def _grok2api_login():
    """登录 grok2api 管理后台，返回 access_token 或 None。"""
    url = f"{GROK2API_BASE}/api/admin/v1/auth/login"
    try:
        r = _requests.post(url, json={"username": GROK2API_USER, "password": GROK2API_PASS}, timeout=15)
        if r.status_code == 200:
            token = r.json().get("data", {}).get("tokens", {}).get("accessToken")
            if token:
                return token
        print(f"  [G2A] 登录失败: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"  [G2A] 登录异常: {e}")
    return None


def push_to_grok2api(email_list, force=False):
    """把 email_list 中的 xai-*.json 上传到 grok2api Build 账号池。
    force=False(默认): 已在 grok2api 的账号跳过 —— 防止用过期的本地 json
    覆盖 grok2api 自行刷新后的新鲜 token(曾导致 token 倒退 + reauthRequired)。
    force=True: 全部覆盖(修复 token 时用)。
    返回 (created, updated, skipped) 或 (0,0,0) on failure。"""
    if not email_list:
        return 0, 0, 0
    # 找到对应文件
    files_to_upload = []
    for email in email_list:
        safe = email.replace("@", "_").replace(".", "_")
        path = os.path.join(AUTH_DIR, f"xai-{safe}.json")
        if os.path.exists(path):
            files_to_upload.append(path)
        else:
            print(f"  [G2A] 文件不存在，跳过: {path}")

    if not files_to_upload:
        print("  [G2A] 无可上传的文件")
        return 0, 0, 0

    if not force:
        # 读 grok2api 本地 DB, 过滤已存在账号(防止旧 json 覆盖新 token)
        db = _grok2api_db()
        try:
            import sqlite3 as _sq
            if os.path.exists(db):
                conn = _sq.connect(db)
                existing = set(r[0] for r in conn.execute(
                    "SELECT email FROM provider_accounts WHERE provider='grok_build'"))
                conn.close()
                before = len(files_to_upload)
                kept = []
                for p in files_to_upload:
                    try:
                        with open(p, encoding="utf-8") as _f:
                            _email = json.load(_f).get("email", "")
                    except Exception:
                        _email = ""
                    if _email not in existing:
                        kept.append(p)
                files_to_upload = kept
                print(f"  [G2A] 跳过 {before - len(files_to_upload)} 个已存在账号(用 --force 可覆盖)")
        except Exception as e:
            print(f"  [G2A] ⚠️ 读取本地 DB 失败，继续全量: {e}")

    if not files_to_upload:
        print("  [G2A] 全部账号已存在，无新上传")
        return 0, 0, 0

    token = _grok2api_login()
    if not token:
        print("  [G2A] ❌ 无法获取管理 token，跳过上传")
        return 0, 0, 0

    url = f"{GROK2API_BASE}/api/admin/v1/accounts/import"
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    created = updated = skipped = 0
    for path in files_to_upload:
        fname = os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                r = _requests.post(url, headers=headers,
                                   files={"file": (fname, fh, "application/json")},
                                   stream=True, timeout=30)
            if r.status_code not in (200, 201):
                print(f"  [G2A] ❌ 上传 {fname}: HTTP {r.status_code} {r.text[:120]}")
                continue
            # 解析 SSE，找 event: complete
            for line in r.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace")
                if text.startswith("data:"):
                    import json as _json
                    try:
                        d = _json.loads(text[5:].strip())
                        created  += d.get("created",  0)
                        updated  += d.get("updated",  0)
                        skipped  += d.get("skipped",  0)
                    except Exception:
                        pass
            print(f"  [G2A] ✅ {fname} 上传成功")
        except Exception as e:
            print(f"  [G2A] ❌ 上传 {fname} 异常: {e}")

    print(f"\n[G2A] 导入结果: created={created} updated={updated} skipped={skipped}")
    return created, updated, skipped


def _assign_web_egress(node_id=1):
    """SQL 直接分配未绑定 egress 的 Web 账号到指定节点。"""
    import sqlite3 as _sqlite3
    db = _grok2api_db()
    try:
        conn = _sqlite3.connect(db)
        cur = conn.cursor()
        # 确保目标 egress 节点存在，否则 grok2api 启动迁移的外键检查会失败
        cur.execute(
            "INSERT OR IGNORE INTO egress_nodes "
            "(id, name, scope, source_key, created_at, updated_at) "
            "VALUES (?, ?, 'grok_web', 'default', datetime('now'), datetime('now'))",
            (node_id, f"default-web-{node_id}"))
        cur.execute(
            "UPDATE provider_accounts SET egress_node_id = ?, egress_assignment_mode = 'manual' "
            "WHERE provider = 'grok_web' AND (egress_node_id IS NULL OR egress_node_id != ?)",
            (node_id, node_id))
        n = cur.rowcount
        conn.commit()
        conn.close()
        if n > 0:
            print(f"  [G2A-Web] ✅ {n} 个 Web 账号已绑定 egress 节点 {node_id}")
        return n
    except Exception as e:
        print(f"  [G2A-Web] ❌ egress 分配失败: {e}")
        return 0


def push_web_to_grok2api(sso_list):
    """把 sso_list (dict列表: {sso, email}) 推入 grok2api Web 账号池。
    返回 (created, updated, skipped) 或 (0,0,0) on failure。"""
    if not sso_list:
        return 0, 0, 0

    token = _grok2api_login()
    if not token:
        print("  [G2A-Web] ❌ 无法获取管理 token，跳过上传")
        return 0, 0, 0

    url = f"{GROK2API_BASE}/api/admin/v1/accounts/web/import"
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    # 逐个导入 (2026-08-07: 批量 5 个一文件实测只成功 1/3, 逐个导入 100% 成功)
    created = updated = skipped = 0
    for item in sso_list:
        content = item["sso"].encode("utf-8")
        ok = False
        for attempt in (1, 2):
            try:
                r = _requests.post(url, headers=headers,
                                   files={"file": (f"sso.txt", content, "text/plain")},
                                   stream=True, timeout=60)
                if r.status_code not in (200, 201):
                    print(f"  [G2A-Web] ❌ {item['email'][:25]} 上传失败: HTTP {r.status_code} {r.text[:120]}" +
                          ("" if attempt == 2 else ", 重试..."))
                    continue
                for line in r.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace")
                    if text.startswith("data:"):
                        try:
                            d = json.loads(text[5:].strip())
                            created += d.get("created", 0)
                            updated += d.get("updated", 0)
                            skipped += d.get("skipped", 0)
                        except Exception:
                            pass
                ok = True
                print(f"  [G2A-Web] ✅ {item['email'][:25]}")
                break
            except Exception as e:
                print(f"  [G2A-Web] ❌ {item['email'][:25]} 上传异常: {e}" +
                      ("" if attempt == 2 else ", 重试..."))
        if not ok:
            skipped += 1

    # 分配 egress
    if created > 0:
        _assign_web_egress(node_id=1)

    print(f"\n[G2A-Web] 导入结果: created={created} updated={updated} skipped={skipped}")
    return created, updated, skipped


def run_token_refresh():
    """刷新所有 token。

    只调 grok2api 自身的刷新 API(POST /accounts/refresh-tokens)——grok2api
    持有自己的 token 副本, 本地刷新会轮换 refresh_token 导致 grok2api 侧失效
    (400 revoked)。本地 token_daemon 已移除(2026-08-16,防双刷冲突),
    网关不可达时直接报失败,由 grok2api 自治刷新兜底。
    """
    print(f"\n{'='*50}")
    print(f"[STEP 3/3] Token 刷新检查...")
    print(f"{'='*50}")

    token = _grok2api_login()
    if token:
        url = f"{GROK2API_BASE}/api/admin/v1/accounts/refresh-tokens"
        try:
            r = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
            print(f"  [G2A] 刷新请求: HTTP {r.status_code} {r.text[:200]}")
            return r.status_code == 200
        except Exception as e:
            print(f"  [G2A] 刷新调用异常: {e}")

    # 无 grok2api 网关:无法刷新(本地 token_daemon 已移除,刷新单一归属 grok2api)
    print("  [AUTO] ❌ grok2api 不可达,刷新跳过(刷新职责单一归属 grok2api)")
    return False


def count_web_available():
    """统计 grok2api Web 池可用账号数"""
    import sqlite3 as _sq
    db = _grok2api_db()
    if not os.path.exists(db):
        print(f"  [WEB-COUNT] ⚠️ 数据库不存在: {db!r}")
        print(f"  [WEB-COUNT] ⚠️ 检查 GROK2API_DB 是否被 systemd EnvironmentFile 行尾注释污染"
              f"(值里夹带 '#...' 会静默导致 Web 池计数为 0 → 无限注册)")
        return 0, []
    try:
        conn = _sq.connect(db)
        cur = conn.cursor()
        cur.execute(
            "SELECT p.email FROM provider_accounts p "
            "JOIN account_credentials c ON c.account_id = p.id "
            "WHERE p.provider = 'grok_web' AND p.auth_status = 'active' AND c.refresh_permanent = 0 "
            "AND p.egress_node_id IS NOT NULL")
        emails = [r[0] for r in cur.fetchall()]
        conn.close()
        return len(emails), emails
    except Exception as e:
        print(f"  [WEB-COUNT] {e}")
        return 0, []


def push_existing(force=False):
    """推送存量账号到 grok2api（不注册）：auths/ 全部 token → Build 池，accounts.txt 全部 SSO → Web 池。"""
    emails = []
    for fn in os.listdir(AUTH_DIR):
        if fn.startswith("xai-") and fn.endswith(".json"):
            p = os.path.join(AUTH_DIR, fn)
            email = fn[4:-5]  # xai- 前缀 / .json 后缀
            try:
                with open(p, encoding="utf-8") as fh:
                    email = json.load(fh).get("email") or email
            except Exception:
                pass
            emails.append(email)
    if emails:
        print(f"\n{'='*50}")
        print(f"[PUSH] Build 池: 推送存量 token ({len(emails)} 个)...")
        print(f"{'='*50}")
        push_to_grok2api(emails, force=force)
    else:
        print("[PUSH] auths/ 无存量 token，跳过 Build 池")

    sso_list = []
    for l in _file_lines(ACCOUNTS_TXT):
        parts = l.split(":")
        if len(parts) >= 3:
            sso_list.append({"email": parts[0], "sso": ":".join(parts[2:])})
    if sso_list:
        print(f"\n{'='*50}")
        print(f"[PUSH] Web 池: 推送存量 SSO ({len(sso_list)} 个)...")
        print(f"{'='*50}")
        push_web_to_grok2api(sso_list)
    else:
        print("[PUSH] accounts.txt 无 SSO，跳过 Web 池")


def purge_build_pool():
    """删除 grok2api Build 池全部账号（重推前清场，不删除 Web/Console 池）。"""
    token = _grok2api_login()
    if not token:
        print("[PURGE] ❌ 无法获取管理 token，检查 .env 的 GROK2API_USER/PASS")
        return False
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GROK2API_BASE}/api/admin/v1/accounts?provider=grok_build&pageSize=100"
    try:
        r = _requests.get(url, headers=headers, timeout=15)
        d = r.json().get("data", {})
    except Exception as e:
        print(f"[PURGE] ❌ 读取账号失败: {e}")
        return False
    items = d.get("items", [])
    print(f"[PURGE] Build 池账号: {len(items)} 个 (total={d.get('total')})")
    for it in items:
        print(f"  id={it.get('id')} {str(it.get('email', '?')):35s} "
              f"authStatus={it.get('authStatus')} refreshable={it.get('refreshable')}")
    if not items:
        print("[PURGE] 无账号可删")
        return True
    try:
        r2 = _requests.delete(f"{GROK2API_BASE}/api/admin/v1/accounts", headers=headers,
                              json={"ids": [it["id"] for it in items], "provider": "grok_build"},
                              timeout=15)
        print(f"[PURGE] 删除: HTTP {r2.status_code} {r2.text[:300]}")
        return r2.status_code == 200
    except Exception as e:
        print(f"[PURGE] ❌ 删除失败: {e}")
        return False


def replenish(min_accounts=2, min_web=None, rotate_region=False, use_free=False):
    """一次完整补位周期。Build 为主池、Web 为副池:Web 门槛默认同 Build,可单独调低。
    返回 (ok, grew):ok=是否达标,grew=本轮两池是否有净增长(供熔断用)。"""
    log = logging.getLogger("replenish")
    min_web = min_web if min_web and min_web > 0 else min_accounts
    count, emails = count_available()
    web_count, web_emails = count_web_available()
    log.info("补位周期: Build=%d(阈值%d) Web=%d(阈值%d)", count, min_accounts, web_count, min_web)
    print(f"\n[{time.strftime('%H:%M:%S')}] Build池: {count} | Web池: {web_count}")
    if emails:
        print(f"  Build: {', '.join(emails[:5])}{'...' if len(emails)>5 else ''}")
    if web_emails:
        print(f"  Web:   {', '.join(e[:25] for e in web_emails[:5])}{'...' if len(web_emails)>5 else ''}")

    if count >= min_accounts and web_count >= min_web:
        print(f"  ✅ 账号充足 (Build {count}>={min_accounts}, Web {web_count}>={min_web})，无需补位")
        print(f"  🔄 检查 token 刷新...")
        run_token_refresh()
        return True, True

    # ── 快照当前节点 ──
    original_node = None
    if clash_available():
        try:
            original_node = snapshot()
            print(f"[IP] 快照节点: {original_node[:40]}")
        except Exception as e:
            print(f"[IP] ⚠️ 快照失败: {e}")

    shortage = max(min_accounts - count, min_web - web_count)
    to_register = min(shortage + 1, REGISTER_CAP)
    print(f"  ⚠️ 账号不足 (Build {count}<{min_accounts} 或 Web {web_count}<{min_web})，需补 {shortage} 个（将注册 {to_register} 个）")

    # ── 烧钱防护(2026-08-15)──
    # Web 池不足且 grok2api 不可达时,注册了也推不进去 → SSO 滞留 keys/accounts.txt,
    # 下一轮照旧触发补位 → YesCaptcha/LuckMail 无限烧。
    # 当天早上 grok2api 登录 404 时的 12 个滞留号就是这么来的。
    if web_count < min_web and not _grok2api_login():
        print("[AUTO] ⚠️ Web 池不足但 grok2api 不可达 → 跳过注册(防止烧 YesCaptcha/邮箱配额)")
        log.warning("Web 池不足(%d<%d)且 grok2api 不可达,跳过注册", web_count, min_web)
        return False, False

    try:
        # Step 1: 注册 + 收集 SSO
        reg_ok, sso_list = run_registration(to_register, rotate_region=rotate_region, use_free=use_free)
        if not reg_ok:
            print("[AUTO] ❌ 注册失败，跳过后续步骤")
            return False, False

        time.sleep(3)

        # Step 2: 推入 grok2api Web 池 (SSO 直推, 不依赖 OAuth PKCE)
        if sso_list:
            print(f"\n{'='*50}")
            print(f"[STEP 2/5] 推入 grok2api Web 池 ({len(sso_list)} 个)...")
            print(f"{'='*50}")
            push_web_to_grok2api(sso_list)

        # Step 3: Device Flow 铸造 Build 池（可能失败, 不影响 Web 池）
        if sso_list:
            convert_sso_list(sso_list)

        # Step 4: Token 刷新
        time.sleep(2)
        run_token_refresh()

        # Step 5: 推入 grok2api Build 账号池（依赖 OAuth 转换, Web 池已先行）
        if sso_list:
            print(f"\n{'='*50}")
            print(f"[STEP 5/5] 推入 grok2api Build 池 ({len(sso_list)} 个)...")
            print(f"{'='*50}")
            push_to_grok2api([item["email"] for item in sso_list])

        # 最终验证
        time.sleep(2)
        new_count, new_emails = count_available()
        new_web_count, new_web_emails = count_web_available()
        print(f"\n{'='*50}")
        print(f"补位结束。Build池: {count} → {new_count} | Web池: {new_web_count}")
        for e in new_emails:
            print(f"  ✅ Build: {e}")
        for e in new_web_emails:
            print(f"  ✅ Web:   {e}")

        grew = (new_count > count) or (new_web_count > web_count)
        if new_count >= min_accounts and new_web_count >= min_web:
            print(f"[AUTO] ✅ 补位成功 (Build {new_count}>={min_accounts}, Web {new_web_count}>={min_web})")
            return True, grew
        else:
            print(f"[AUTO] ⚠️ 补位不足 (Build {new_count}<{min_accounts} 或 Web {new_web_count}<{min_web})，可能需要再次运行")
            return False, grew

    finally:
        # ── 恢复原始节点 ──
        if original_node and clash_available():
            try:
                restore(original_node)
            except Exception as e:
                print(f"[IP] ⚠️ 恢复节点失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="Grok 账号自动补位系统")
    parser.add_argument("--min", type=int, default=int(os.getenv("GROK_MIN_ACCOUNTS", "2")),
                        help="最少可用账号数（默认取 .env GROK_MIN_ACCOUNTS，兜底 2）")
    parser.add_argument("--min-web", type=int, default=int(os.getenv("GROK_MIN_WEB_ACCOUNTS", "0")),
                        help="Web 池最低账号数(副池,默认同 --min,可调低如 30)")
    parser.add_argument("--daemon", type=int, default=0, help="守护模式，每 N 秒检查一次")
    parser.add_argument("--check", action="store_true", help="仅检查状态")
    parser.add_argument("--rotate-region", action="store_true", help="跨区域切换 IP（而非随机节点）")
    parser.add_argument("--refresh-only", action="store_true", help="仅刷新 token，不注册")
    parser.add_argument("--push-existing", action="store_true",
                        help="推送存量 auths/ token 与 accounts.txt SSO 到 grok2api，不注册")
    parser.add_argument("--force", action="store_true",
                        help="与 --push-existing 同用: 强制覆盖已存在账号的 token(默认跳过，防止旧 json 覆盖新 token)")
    parser.add_argument("--purge-build", action="store_true",
                        help="删除 grok2api Build 池全部账号（清场后重推）")
    parser.add_argument("--free", action="store_true", help="使用免费 DrissionPage 注册（默认用 YesCaptcha）")
    parser.add_argument("--free-min", type=int, default=int(os.getenv("GROK_MIN_FREE_ACCOUNTS", "200")),
                        help="免费路径水位(低于此用免费路径,默认 200)")
    args = parser.parse_args()

    setup_logging("replenish.log")
    log = logging.getLogger("replenish")

    if args.check:
        count, emails = count_available()
        web_count, web_emails = count_web_available()
        print(f"=== Grok 账号状态 ===")
        print(f"Build 池: {count} 可用")
        for e in emails:
            print(f"  ✅ {e}")
        print(f"Web 池:   {web_count} 可用")
        for e in web_emails:
            print(f"  ✅ {e}")

        if clash_available():
            try:
                h = clash_health()
                if h["ok"]:
                    print(f"\nClash 代理: ✅")
                    print(f"  当前节点: {h['current_node'][:50]}")
                    print(f"  出口 IP: {h['current_ip']}")
                    print(f"  区域: {h['region']}")
                else:
                    print(f"\nClash 代理: ❌ {h.get('error','?')}")
            except Exception as e:
                print(f"\nClash 代理: ❌ {e}")
        return

    if args.refresh_only:
        run_token_refresh()
        return

    if args.push_existing:
        push_existing(force=args.force)
        return

    if args.purge_build:
        purge_build_pool()
        return

    if args.daemon:
        paid_min = args.min
        free_min = args.free_min
        min_web = args.min_web if args.min_web > 0 else args.min
        print(f"[DAEMON] 每 {args.daemon}s | 付费保底 {paid_min} | 免费水位 {free_min} | Web ≥ {min_web} | "
              f"区域轮换={'✅' if args.rotate_region else '❌'}")
        stale = 0  # 连续零增长轮数 → 熔断防烧钱
        while True:
            try:
                count, _ = count_available()
                web_count, _ = count_web_available()
                free_used = False
                if count < paid_min:
                    print(f"[DAEMON] Build {count} < 付费保底 {paid_min} → 付费路径(YesCaptcha)")
                    _ok, grew = replenish(paid_min, min_web=min_web,
                                          rotate_region=args.rotate_region, use_free=False)
                elif count < free_min or web_count < min_web:
                    print(f"[DAEMON] Build {count} < 免费水位 {free_min} 或 Web {web_count} < {min_web}"
                          f" → 免费路径(表单+轮换IP)")
                    free_used = True
                    _ok, grew = replenish(free_min, min_web=min_web,
                                          rotate_region=args.rotate_region, use_free=True)
                else:
                    print(f"[DAEMON] Build {count} ≥ {free_min} 且 Web {web_count} ≥ {min_web},休眠")
                    grew = True
                if grew:
                    if stale >= 3:
                        # 恢复后通知(冷却去重)
                        try:
                            from alert import notify
                            notify("recovered", f"Grok 补位恢复(Build {count} → 有增长)",
                                   cooldown=86400)
                        except Exception:
                            pass
                    stale = 0
                else:
                    stale += 1
                    if FREE_CIRCUIT_STREAK > 0 and stale >= FREE_CIRCUIT_STREAK:
                        # 二级长熔断:连续 N 轮零增长 → 熔断到次日 + TG 推送(带日志分析)
                        print(f"[DAEMON] 🔴 连续 {stale} 轮零增长,长熔断休眠 {FREE_CIRCUIT_SLEEP}s(到次日)")
                        log.warning("连续 %d 轮零增长,长熔断 %d 秒", stale, FREE_CIRCUIT_SLEEP)
                        try:
                            from alert import notify, health_summary
                            notify("circuit", f"Grok 连续 {stale} 轮零增长,熔断到次日",
                                   f"池 Build={count} Web={web_count}\n\n" + health_summary(),
                                   cooldown=86400)
                        except Exception as e:
                            print(f"  [ALERT] 推送异常: {e}")
                        time.sleep(FREE_CIRCUIT_SLEEP)
                        stale = 0
                    elif stale >= 3:
                        print(f"[DAEMON] ⚠️ 连续 {stale} 轮零增长,暂停补位 30 分钟(防烧钱)")
                        log.warning("连续 %d 轮零增长,暂停补位 30 分钟", stale)
                        time.sleep(1800)
                        stale = 0
                    elif count < free_min and free_used:
                        # 免费路径零增长 = 抑制窗口(24h 日预算耗尽):
                        # 长休眠等恢复,避免窗口内反复烧邮箱(每次尝试 0.02)
                        print(f"[DAEMON] ⚠️ 免费路径零增长,休眠 {FREE_SUPPRESSION_SLEEP}s 等抑制窗口")
                        log.warning("免费路径零增长,休眠 %d 秒", FREE_SUPPRESSION_SLEEP)
                        # 首次检测到抑制时推送一次(冷却去重,避免每轮刷屏)
                        try:
                            from alert import notify
                            notify("suppressed", f"Grok 注册被抑制(疑似 x.ai 发码配额耗尽)",
                                   f"池 Build={count} Web={web_count}", cooldown=43200)
                        except Exception:
                            pass
                        time.sleep(FREE_SUPPRESSION_SLEEP)
            except KeyboardInterrupt:
                print("\n[DAEMON] 退出")
                break
            except Exception as e:
                print(f"[DAEMON] 错误: {e}")
                log.exception("补位循环异常")
                stale += 1
            time.sleep(args.daemon)
    else:
        replenish(args.min, min_web=args.min_web if args.min_web > 0 else args.min,
                  rotate_region=args.rotate_region, use_free=args.free)


if __name__ == "__main__":
    main()
