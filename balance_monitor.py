"""余额监控:定时检测 LuckMail 余额 + YesCaptcha 点数。

低于阈值 → 关闭自动补水 + 发邮件告警 + 自写代码的日志异常分析。

配置(.env):
  LUCKMAIL_MIN_BALANCE=1.0       # LuckMail 余额阈值(默认 1.0)
  YESCAPTCHA_MIN_POINTS=800      # YesCaptcha 点数阈值(默认 800)
  ALERT_EMAIL_TO=you@example.com        # 告警收件人
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM   # 发件 SMTP(QQ/foxmail 用 smtp.qq.com:465)
  GROK_STOP_CMD=systemctl stop vps-grok-replenish   # 关补水的命令(VPS)

日志分析:不调大模型,纯代码分析 logs/ 下最近 24h 日志:
成功/失败计数、错误模式提取、连续失败告警。
"""
import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
from collections import Counter
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
from config import LOG_DIR, setup_logging

log = setup_logging("balance-monitor.log")

MIN_LUCKMAIL = float(os.getenv("LUCKMAIL_MIN_BALANCE", "1.0"))
MIN_YESCAPTCHA = float(os.getenv("YESCAPTCHA_MIN_POINTS", "800"))
ALERT_TO = os.getenv("ALERT_EMAIL_TO", "").strip()
STOP_CMD = os.getenv("GROK_STOP_CMD", "").strip()


# ── 余额获取 ──

def luckmail_balance() -> float:
    """LuckMail 余额(API Key)。失败抛异常。"""
    import requests as _r
    key = os.getenv("LUCKMAIL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 LUCKMAIL_API_KEY")
    base = os.getenv("LUCKMAIL_BASE_URL", "https://mails.luckyous.com").strip().rstrip("/")
    r = _r.get(f"{base}/api/v1/openapi/balance",
               headers={"X-API-Key": key}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"LuckMail 余额查询失败: {data}")
    bal = data.get("data") or {}
    if isinstance(bal, dict):
        bal = bal.get("balance", 0)
    return float(bal)


def yescaptcha_balance() -> float:
    """YesCaptcha 点数(getBalance,2captcha 兼容)。失败抛异常。"""
    import requests as _r
    key = os.getenv("YESCAPTCHA_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 YESCAPTCHA_KEY")
    r = _r.post("https://api.yescaptcha.com/getBalance",
                json={"clientKey": key}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("errorId") != 0:
        raise RuntimeError(f"YesCaptcha 余额查询失败: {data.get('errorCode')}")
    return float(data.get("balance", 0))


# ── 日志异常分析(自写代码,不调大模型) ──

# ── 告警(2026-08-16:复用共享模块 alert.py,Telegram 优先 + 冷却去重) ──

def send_alert(subject: str, body: str) -> bool:
    from alert import notify
    return notify("balance", subject, body, cooldown=86400)


def health_summary() -> str:
    from alert import health_summary as _hs
    return _hs()


# ── 关补水 ──

def stop_replenishment(reason: str):
    """执行 GROK_STOP_CMD(默认 systemctl stop)。失败则写本地停止标记。"""
    if STOP_CMD:
        try:
            import subprocess
            r = subprocess.run(STOP_CMD, shell=True, capture_output=True, text=True, timeout=30)
            log.warning("已执行关补水命令: %s → rc=%d %s", STOP_CMD, r.returncode, r.stderr[:100])
            return
        except Exception as e:
            log.error("关补水命令执行失败: %s", e)
    # 兜底:写标记文件(daemon 侧可检查)
    marker = os.path.join(SCRIPT_DIR, ".stop_replenish")
    with open(marker, "w", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {reason}\n")
    log.critical("已写停止标记 %s (原因: %s)", marker, reason)


def main():
    ap = argparse.ArgumentParser(description="余额监控(低于阈值关补水+邮件告警)")
    ap.add_argument("--once", action="store_true", help="只跑一次(供 timer 调用)")
    ap.add_argument("--interval", type=int, default=3600, help="循环间隔秒(默认 3600)")
    ap.add_argument("--check", action="store_true", help="仅查询余额并打印(不动作)")
    args = ap.parse_args()

    def _cycle():
        findings = []
        try:
            lb = luckmail_balance()
            log.info("LuckMail 余额: %.4f", lb)
            findings.append(f"LuckMail 余额: {lb:.4f} (阈值 {MIN_LUCKMAIL})")
        except Exception as e:
            lb = None
            log.error("LuckMail 余额查询失败: %s", e)
            findings.append(f"LuckMail 余额: 查询失败({e})")

        try:
            yb = yescaptcha_balance()
            log.info("YesCaptcha 点数: %.0f", yb)
            findings.append(f"YesCaptcha 点数: {yb:.0f} (阈值 {MIN_YESCAPTCHA})")
        except Exception as e:
            yb = None
            log.error("YesCaptcha 余额查询失败: %s", e)
            findings.append(f"YesCaptcha 点数: 查询失败({e})")

        if args.check:
            print("\n".join(findings))
            print("\n" + health_summary())
            return

        # 触发条件:LuckMail < 1 或 YesCaptcha < 800
        triggers = []
        if lb is not None and lb < MIN_LUCKMAIL:
            triggers.append(f"LuckMail 余额 {lb:.4f} < {MIN_LUCKMAIL}")
        if yb is not None and yb < MIN_YESCAPTCHA:
            triggers.append(f"YesCaptcha 点数 {yb:.0f} < {MIN_YESCAPTCHA}")
        if not triggers:
            log.info("余额充足,无需动作")
            return

        reason = "; ".join(triggers)
        log.critical("余额告警触发: %s", reason)
        stop_replenishment(reason)
        body = (
            "Grok 补位系统余额告警\n\n"
            + "\n".join(findings)
            + "\n\n触发动作: 已停止自动补水\n\n"
            + "=== 日志健康分析(最近24h) ===\n"
            + health_summary()
        )
        send_alert("[Grok] 余额告警: " + reason, body)

    if args.once:
        _cycle()
        return
    while True:
        try:
            _cycle()
        except Exception as e:
            log.exception("监控循环异常: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
