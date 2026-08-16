"""共享告警模块(2026-08-16):Telegram 推送 + 冷却去重 + 日志健康分析。

balance_monitor.py(余额)与 auto_replenish.py(注册失败/熔断)共用,
避免各自实现 Telegram 发送与告警频率控制。

配置(.env):
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   # Telegram 推送(@BotFather 建 bot)
  SMTP_HOST/PORT/USER/PASS/FROM           # SMTP 兜底(可选)
  ALERT_EMAIL_TO                          # SMTP 收件人
  ALERT_COOLDOWN_SECONDS                  # 同类告警冷却(默认 3600,防刷屏)
"""
import logging
import os
import re
import time
from collections import Counter
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
import smtplib
import ssl

from dotenv import load_dotenv

load_dotenv(override=True)

from config import LOG_DIR

log = logging.getLogger("alert")

COOLDOWN = int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600"))
ALERT_TO = os.getenv("ALERT_EMAIL_TO", "").strip()
_STATE_FILE = os.path.join(LOG_DIR, ".alert_state.json")

ERROR_PATTERNS = [
    (re.compile(r"Traceback \(most recent call last\)"), "异常栈"),
    (re.compile(r"(?:ERROR|CRITICAL)\s"), "ERROR级日志"),
    (re.compile(r"未收到验证码|发送验证码失败|验证码无效|Turnstile 未解决"), "注册失败"),
    (re.compile(r"铸造失败|浏览器启动失败"), "铸造失败"),
    (re.compile(r"HTTP 40[34]|403 Forbidden|Attention Required"), "CF/HTTP拦截"),
    (re.compile(r"余额不足|Insufficient balance|2001"), "余额不足"),
    (re.compile(r"连续 .* 轮零增长|提前终止本轮"), "熔断/抑制"),
]


# ── 日志健康分析(自写代码,不调大模型) ──

def analyze_logs(hours: int = 24) -> dict:
    """扫描 LOG_DIR 最近 hours 小时日志,返回健康摘要。"""
    now = time.time()
    cutoff = now - hours * 3600
    result = {"files": 0, "lines": 0, "ok_lines": 0, "error_counts": Counter(),
              "error_samples": [], "recent_errors": 0, "last_error_time": None}
    log_path = Path(LOG_DIR)
    if not log_path.is_dir():
        return result
    for f in sorted(log_path.glob("*.log")):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            result["files"] += 1
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                result["lines"] += 1
                if any(kw in line for kw in ("✅", "成功", "已保存", "SSO", "OK")):
                    result["ok_lines"] += 1
                for pat, name in ERROR_PATTERNS:
                    if pat.search(line):
                        result["error_counts"][name] += 1
                        result["recent_errors"] += 1
                        result["last_error_time"] = line[:19]
                        if len(result["error_samples"]) < 5:
                            result["error_samples"].append(line[:160])
                        break
        except (OSError, UnicodeDecodeError):
            continue
    return result


def health_summary(hours: int = 24) -> str:
    """人类可读健康摘要(告警正文用)。"""
    a = analyze_logs(hours)
    lines = [
        f"日志文件: {a['files']} 个,共 {a['lines']} 行",
        f"成功标记: {a['ok_lines']} 行 | 错误行: {a['recent_errors']} 行"
        f"(最近: {a['last_error_time'] or '无'})",
    ]
    if a["error_counts"]:
        lines.append("错误分类:")
        for name, n in a["error_counts"].most_common():
            lines.append(f"  • {name}: {n}")
    if a["error_samples"]:
        lines.append("错误样例:")
        for s in a["error_samples"][:3]:
            lines.append(f"  > {s}")
    return "\n".join(lines)


# ── 发送通道 ──

def send_telegram(text: str) -> bool:
    """Telegram Bot API 推送。配置 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID。"""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        log.warning("Telegram 未配置(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID),跳过推送")
        return False
    import requests as _r
    try:
        r = _r.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            log.info("Telegram 推送成功")
            return True
        log.error("Telegram 推送失败: %s", data.get("description", r.status_code))
        return False
    except Exception as e:
        log.error("Telegram 推送异常: %s", e)
        return False


def _smtp_send(subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    pwd = os.getenv("SMTP_PASS", "").strip()
    if not (host and user and pwd and ALERT_TO):
        return False
    port = int(os.getenv("SMTP_PORT", "465"))
    frm = os.getenv("SMTP_FROM", user).strip()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = frm
    msg["To"] = ALERT_TO
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
            s.login(user, pwd)
            s.sendmail(frm, [ALERT_TO], msg.as_string())
        log.info("告警邮件已发送: %s", subject)
        return True
    except Exception as e:
        log.error("邮件发送失败: %s", e)
        return False


# ── 冷却去重 ──

def _load_state() -> dict:
    try:
        import json
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    import json
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


def notify(kind: str, subject: str, body: str = "", cooldown: int = COOLDOWN) -> bool:
    """冷却去重的告警发送:同类告警 cooldown 秒内只发一次。

    Telegram 优先,SMTP 兜底。返回是否真的发出。"""
    state = _load_state()
    last = state.get(kind, 0)
    now = time.time()
    if now - last < cooldown:
        log.info("[alert] %s 冷却期内(%.0fs),跳过推送", kind, now - last)
        return False
    sent = send_telegram(f"⚠️ {subject}\n\n{body}" if body else f"⚠️ {subject}")
    if not sent:
        sent = _smtp_send(subject, body)
    if sent:
        state[kind] = now
        _save_state(state)
        log.warning("[alert] %s 已推送", kind)
    return sent
