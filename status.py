#!/usr/bin/env python3
"""grok-register 状态总览:池水位 / 注册 / 铸造 / 刷新 / 重授权 / API 成功率 / 余额 / 服务。

用法:
  python status.py               # 人类可读完整报告
  python status.py --json        # 机器可读 JSON(给脚本/AI)
  python status.py --section pool   # 只看某个板块:pool|register|mint|refresh|reauth|api|balance|services|nodes|alerts
  python status.py --days 7      # API 审计/新增统计按 N 天窗口(默认 1)

数据源(全部只读,不写任何状态):
  - grok2api SQLite (GROK2API_DB):账号状态/凭据刷新/API 审计/egress 节点
  - keys/accounts.txt:累计注册 SSO
  - auths/:铸造产物(文件时间 = 铸造时间)
  - logs/:各 run 日志(注册成功/失败计数)
  - systemctl:本机服务状态(best effort)
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from config import AUTH_DIR, KEYS_DIR, LOG_DIR  # noqa: E402

DB_PATH = os.getenv("GROK2API_DB") or os.path.join(os.getcwd(), "data", "backend.db")
NOW = datetime.now(timezone.utc)

RE_LOG_OK = re.compile(r"✅ \((\d+)/(\d+) 成功\)")
RE_LOG_SUMMARY = re.compile(r"成功=(\d+) 失败=(\d+)")
RE_BALANCE_LUCKMAIL = re.compile(r"LuckMail 余额:\s*([\d.]+)")
RE_BALANCE_YC = re.compile(r"YesCaptcha 点数:\s*(\d+)")
RE_BALANCE_VERDICT = re.compile(r"(余额充足,无需动作|触发.*|低于阈值.*|余额不足.*)")


# ── 通用 ──

def db_conn():
    if not os.path.exists(DB_PATH):
        return None
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def systemctl_active(unit):
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode in (0, 3) else "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def jwt_sub(token):
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("sub")
    except Exception:
        return None


def _short(s, n=28):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ── 各板块 ──

def sec_pool():
    conn = db_conn()
    out = {"status": {}, "verdict": "", "nodes": []}
    if conn is None:
        out["error"] = f"GROK2API_DB 不可用: {DB_PATH}"
        return out
    rows = conn.execute(
        "SELECT provider, auth_status, COUNT(*) FROM provider_accounts GROUP BY 1,2").fetchall()
    conn.close()
    by = {}
    for prov, st, n in rows:
        by.setdefault(prov, {})[st] = n
    build = by.get("grok_build", {})
    web = by.get("grok_web", {})
    build_active = build.get("active", 0)
    web_active = web.get("active", 0)
    pay = int(os.getenv("GROK_MIN_ACCOUNTS") or 2)
    free = int(os.getenv("GROK_MIN_FREE_ACCOUNTS") or 100)
    web_thr = int(os.getenv("GROK_MIN_WEB_ACCOUNTS") or 30)
    eff = max(pay, free)
    build_ok = build_active >= eff
    web_ok = web_active >= web_thr
    out["status"] = {
        "build_active": build_active, "build_water_level": eff,
        "web_active": web_active, "web_water_level": web_thr,
        "thresholds": {"GROK_MIN_ACCOUNTS": pay, "GROK_MIN_FREE_ACCOUNTS": free, "GROK_MIN_WEB_ACCOUNTS": web_thr},
        "build_ok": build_ok, "web_ok": web_ok,
        "by_status": by,
    }
    if build_ok and web_ok:
        out["verdict"] = "充足,补位守护休眠"
    elif build_ok or web_ok:
        out["verdict"] = "一侧不足,需要补位"
    else:
        out["verdict"] = "双池告急,立即补位"
    # 节点分配
    conn = db_conn()
    if conn is not None:
        out["nodes"] = [{"egress_node_id": r[0], "accounts": r[1]}
                        for r in conn.execute(
                            "SELECT egress_node_id, COUNT(*) FROM provider_accounts "
                            "WHERE enabled=1 AND egress_node_id IS NOT NULL GROUP BY 1").fetchall()]
        conn.close()
    return out


def sec_register(days):
    out = {"sso_total": 0, "window": days, "logs": [], "ok": 0, "fail": 0}
    acct = os.path.join(KEYS_DIR, "accounts.txt")
    if os.path.exists(acct):
        out["sso_total"] = sum(1 for _ in open(acct, encoding="utf-8-sig"))
    cutoff = NOW - timedelta(days=days)
    if os.path.isdir(LOG_DIR):
        for fn in sorted(os.listdir(LOG_DIR)):
            fp = os.path.join(LOG_DIR, fn)
            if not fn.endswith(".log"):
                continue
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(fp), timezone.utc)
            except OSError:
                continue
            if mt < cutoff:
                continue
            ok_run = []
            fail_run = 0
            for line in open(fp, encoding="utf-8", errors="replace"):
                m = RE_LOG_OK.search(line)
                if m:
                    ok_run.append(int(m.group(1)))
                s = RE_LOG_SUMMARY.search(line)
                if s:
                    ok_run.append(int(s.group(1)))
                    fail_run += int(s.group(2))
            ok = max(ok_run) if ok_run else 0
            out["ok"] += ok
            out["fail"] += fail_run
            if ok or fail_run:
                out["logs"].append({"file": fn, "day": mt.strftime("%Y-%m-%d"), "ok": ok, "fail": fail_run})
    if out["ok"] or out["fail"]:
        out["success_rate"] = round(100.0 * out["ok"] / (out["ok"] + out["fail"]), 1)
    else:
        out["success_rate"] = None
    return out


def sec_mint(days):
    out = {"total": 0, "window_days": days, "new_in_window": 0, "stale": 0, "unique_subs": None}
    cutoff = NOW - timedelta(days=days)
    files = []
    if os.path.isdir(AUTH_DIR):
        for fn in os.listdir(AUTH_DIR):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(AUTH_DIR, fn)
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(fp), timezone.utc)
            except OSError:
                continue
            files.append((fp, mt))
    out["total"] = len(files)
    out["new_in_window"] = sum(1 for _, mt in files if mt >= cutoff)
    subs = set()
    for fp, _ in files:
        try:
            data = json.load(open(fp, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sub = jwt_sub(data.get("access_token", ""))
        if sub:
            subs.add(sub)
        exp = data.get("expired", "")
        if exp:
            try:
                if datetime.fromisoformat(exp.replace("Z", "+00:00")) < NOW:
                    out["stale"] += 1
            except ValueError:
                pass
    if subs:
        out["unique_subs"] = len(subs)
    return out


def sec_refresh():
    conn = db_conn()
    out: dict = {"error": None, "total": 0, "fresh": 0, "expired": 0, "no_expiry": 0,
                 "failing": 0, "permanent": 0, "last_errors": []}
    if conn is None:
        out["error"] = f"GROK2API_DB 不可用: {DB_PATH}"
        return out
    rows = conn.execute(
        "SELECT expires_at, refresh_failures, refresh_permanent, last_refresh_error "
        "FROM account_credentials").fetchall()
    conn.close()
    now_s = NOW.strftime("%Y-%m-%d %H:%M:%S")
    errs = {}
    for exp, fails, perm, code in rows:
        out["total"] += 1
        if not exp:
            out["no_expiry"] += 1            # 无过期时间 = SSO/常驻类凭据,非过期
        elif exp > now_s:
            out["fresh"] += 1
        else:
            out["expired"] += 1
        if (fails or 0) > 0:
            out["failing"] += 1
        if perm:
            out["permanent"] += 1
        if code:
            errs[code] = errs.get(code, 0) + 1
    out["last_errors"] = sorted(errs.items(), key=lambda kv: -kv[1])[:5]
    return out


def sec_reauth():
    conn = db_conn()
    out: dict = {"pending": None, "service": systemctl_active("vps-grok-reauth")}
    if conn is not None:
        r = conn.execute("SELECT COUNT(*) FROM provider_accounts WHERE auth_status='reauthRequired'").fetchone()
        conn.close()
        out["pending"] = r[0]
    return out


def sec_api(days):
    conn = db_conn()
    out: dict = {"window_days": days, "error": None}
    if conn is None:
        out["error"] = f"GROK2API_DB 不可用: {DB_PATH}"
        return out
    since = (NOW - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT status_code, model_public_id, client_key_name, duration_ms, total_tokens, reasoning_tokens, error_code "
        "FROM request_audits WHERE created_at >= ?", (since,)).fetchall()
    conn.close()
    out["total"] = len(rows)
    out["ok"] = sum(1 for r in rows if r[0] < 400)
    out["fail"] = out["total"] - out["ok"]
    out["success_rate"] = round(100.0 * out["ok"] / out["total"], 1) if out["total"] else None
    codes = {}
    models = {}
    clients = {}
    durations = [r[3] for r in rows if r[3] is not None]
    for status, model, client, *_ in rows:
        codes[str(status)] = codes.get(str(status), 0) + 1
        if model:
            models[model] = models.get(model, 0) + 1
        if client:
            clients[client] = clients.get(client, 0) + 1
    out["status_codes"] = sorted(codes.items(), key=lambda kv: -kv[1])
    out["top_models"] = sorted(models.items(), key=lambda kv: -kv[1])[:5]
    out["top_clients"] = sorted(clients.items(), key=lambda kv: -kv[1])[:3]
    out["avg_duration_ms"] = round(sum(durations) / len(durations)) if durations else None
    return out


def sec_balance():
    out = {"luckmail": None, "yescaptcha": None, "verdict": None, "error": None}
    fp = os.path.join(LOG_DIR, "balance-monitor.log")
    if not os.path.exists(fp):
        out["error"] = f"无 {fp}(balance_monitor 未跑过)"
        return out
    luck, yc, verdict = None, None, None
    for line in open(fp, encoding="utf-8", errors="replace"):
        m = RE_BALANCE_LUCKMAIL.search(line)
        if m:
            luck = m.group(1)
        m = RE_BALANCE_YC.search(line)
        if m:
            yc = m.group(1)
        m = RE_BALANCE_VERDICT.search(line)
        if m:
            verdict = m.group(1)
    out["luckmail"], out["yescaptcha"], out["verdict"] = luck, yc, verdict
    return out


def sec_services():
    units = ["vps-mihomo", "vps-grok2api", "vps-grok-replenish",
             "vps-grok-reauth", "vps-balance-monitor.timer", "vps-resume-replenish.timer"]
    out = {}
    for u in units:
        st = systemctl_active(u)
        if st != "unknown":
            out[u] = st
    return out


def sec_nodes():
    conn = db_conn()
    out: dict = {"error": None, "nodes": []}
    if conn is None:
        out["error"] = f"GROK2API_DB 不可用: {DB_PATH}"
        return out
    rows = conn.execute(
        "SELECT id, name, scope, enabled, proxy_pool, encrypted_proxy_url IS NOT NULL AND encrypted_proxy_url != '', "
        "health, probe_status, cooldown_until, exit_ip, last_error "
        "FROM egress_nodes ORDER BY id").fetchall()
    conn.close()
    for r in rows:
        out["nodes"].append({
            "id": r[0], "name": r[1], "scope": r[2], "enabled": bool(r[3]),
            "proxy_pool": bool(r[4]), "proxy_configured": bool(r[5]), "health": r[6],
            "probe_status": r[7], "cooldown_until": r[8], "exit_ip": r[9], "last_error": r[10],
        })
    return out


def sec_alerts():
    out = {"telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
           "smtp": bool(os.getenv("SMTP_HOST") and os.getenv("ALERT_EMAIL_TO")),
           "alert_email": os.getenv("ALERT_EMAIL_TO") or ""}
    return out


# ── 渲染 ──

SECTIONS = {"pool": sec_pool, "register": sec_register, "mint": sec_mint, "refresh": sec_refresh,
            "reauth": sec_reauth, "api": sec_api, "balance": sec_balance,
            "services": sec_services, "nodes": sec_nodes, "alerts": sec_alerts}


def section_lines(s, name, days):
    """板块 → 文本行列表(人类可读;同时供 CLI 打印与 Telegram 机器人复用)"""
    lines = [f"── {name} ─" + "─" * max(0, 30 - len(name))]
    if name == "pool":
        if s.get("error"):
            lines.append("  " + s["error"]); return lines
        st = s["status"]
        lines.append(f"  Build {st['build_active']} (水位 {st['build_water_level']}) | "
                     f"Web {st['web_active']} (水位 {st['web_water_level']}) → {s['verdict']}")
        for prov, dist in st["by_status"].items():
            lines.append(f"    {prov}: " + " ".join(f"{k}={v}" for k, v in sorted(dist.items())))
        if s["nodes"]:
            lines.append("    节点分配: " + ", ".join(f"#{n['egress_node_id']}={n['accounts']}" for n in s["nodes"]))
    elif name == "register":
        lines.append(f"  累计 SSO: {s['sso_total']} | 近 {s['window']}d 日志: 成功 {s['ok']} / 失败 {s['fail']}"
                     + (f" (成功率 {s['success_rate']}%)" if s["success_rate"] is not None else ""))
        for lg in s["logs"]:
            lines.append(f"    {lg['day']} {lg['file']}: ok={lg['ok']} fail={lg['fail']}")
    elif name == "mint":
        lines.append(f"  auths/ 共 {s['total']} | 近 {s['window_days']}d 新增 {s['new_in_window']} | "
                     f"过期 {s['stale']}" + (f" | 独立用户 {s['unique_subs']}" if s["unique_subs"] else ""))
    elif name == "refresh":
        if s.get("error"):
            lines.append("  " + s["error"]); return lines
        lines.append(f"  凭据 {s['total']}: fresh {s['fresh']} / 过期 {s['expired']}"
                     + (f" / 无过期时间(SSO类) {s['no_expiry']}" if s.get("no_expiry") else "")
                     + f" | 刷新失败中 {s['failing']} | 永久失效 {s['permanent']}")
        if s["last_errors"]:
            lines.append("    最近错误: " + ", ".join(f"{c}×{n}" for c, n in s["last_errors"]))
    elif name == "reauth":
        pend = s["pending"]
        lines.append(f"  待重授权: {pend if pend is not None else 'N/A'} | 守护服务: {s['service']}")
        if pend:
            lines.append("    → reauth_batch --daemon 会自动处理;等不及可手动: python reauth_batch.py")
    elif name == "api":
        if s.get("error"):
            lines.append("  " + s["error"]); return lines
        lines.append(f"  近 {s['window_days']}d 请求 {s['total']}: 成功 {s['ok']} / 失败 {s['fail']}"
                     + (f" (成功率 {s['success_rate']}%)" if s["success_rate"] is not None else ""))
        if s["status_codes"]:
            lines.append("    状态码: " + ", ".join(f"{c}={n}" for c, n in s["status_codes"]))
        if s["top_models"]:
            lines.append("    模型: " + ", ".join(f"{m}×{n}" for m, n in s["top_models"]))
        if s["avg_duration_ms"]:
            lines.append(f"    平均耗时: {s['avg_duration_ms']}ms")
    elif name == "balance":
        if s.get("error"):
            lines.append("  " + s["error"]); return lines
        lines.append(f"  LuckMail {s['luckmail']} | YesCaptcha {s['yescaptcha']} | 最近判定: {s['verdict']}")
    elif name == "services":
        for u, st in s.items():
            mark = {"active": "●", "inactive": "○", "failed": "✗"}.get(st, "?")
            lines.append(f"  {mark} {u}: {st}")
    elif name == "nodes":
        if s.get("error"):
            lines.append("  " + s["error"]); return lines
        for n in s["nodes"]:
            state = "enabled" if n["enabled"] else "disabled"
            flags = []
            if n["proxy_pool"]:
                flags.append("池")
            if not n["proxy_configured"]:
                flags.append("无代理!")
            if n["cooldown_until"]:
                flags.append(f"冷却至{n['cooldown_until'][:16]}")
            lines.append(f"  #{n['id']} {_short(n['name'])} [{state}]{'(' + ','.join(flags) + ')' if flags else ''} "
                         f"health={n['health']} probe={n['probe_status']} ip={n['exit_ip']}")
            if n["last_error"]:
                lines.append(f"      last_error={_short(n['last_error'], 60)}")
    elif name == "alerts":
        lines.append(f"  Telegram: {'✓' if s['telegram'] else '✗ 未配置'} | SMTP: {'✓' if s['smtp'] else '✗ 未配置'}"
                     + (f" | 收件人: {s['alert_email']}" if s["alert_email"] else ""))
    return lines


def full_text(section="all", days=1):
    """完整/单板块报告文本(CLI 与 Telegram 机器人共用)"""
    parts = [f"grok-register 状态总览  {NOW.strftime('%Y-%m-%d %H:%M %Z')}   (GROK2API_DB={DB_PATH})"]
    for name, fn in SECTIONS.items():
        if section != "all" and name != section:
            continue
        s = fn(days) if name in ("register", "mint", "api") else fn()
        parts.extend(section_lines(s, name, days))
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="grok-register 状态总览")
    ap.add_argument("--json", action="store_true", help="输出 JSON(给脚本/AI)")
    ap.add_argument("--section", choices=list(SECTIONS) + ["all"], default="all")
    ap.add_argument("--days", type=int, default=1, help="审计/新增统计窗口(天)")
    args = ap.parse_args()

    if args.json:
        result = {}
        for name, fn in SECTIONS.items():
            if args.section != "all" and name != args.section:
                continue
            result[name] = fn(args.days) if name in ("register", "mint", "api") else fn()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    print(full_text(args.section, args.days))


if __name__ == "__main__":
    main()
