#!/usr/bin/env python3
"""Telegram 查询机器人:在 TG 里发命令/关键词,机器人回复 grok-register 状态。

用法:
  python tg_bot.py            # 长轮询常驻(需 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
  systemd: deploy/vps-grok-tg-bot.service

命令(支持中英文,首词匹配):
  /status /all 状态 全部       → 完整状态总览
  /pool 水位 池                → 池水位 vs 阈值
  /register 注册               → 注册历史与成功率
  /mint 铸造                   → 铸造历史
  /refresh 刷新                → 凭据刷新状态
  /reauth 重授权               → 待重授权与守护状态
  /api 调用                    → API 成功率
  /balance 余额                → LuckMail/YesCaptcha 余额
  /nodes 节点                  → egress 节点健康
  /services 服务               → systemd 单元状态
  /alerts 告警                 → 告警通道配置
  /help 帮助                   → 本帮助

安全:仅 TELEGRAM_CHAT_ID 本人可查询,他人消息静默忽略。
数据:复用 status.py(只读 SQLite/日志/systemctl)。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 50
MAX_CHUNK = 3500  # Telegram 单条消息上限 4096,留余量

HELP = """grok-register 状态查询机器人
用法:发送以下任一命令/关键词:

/status 或 状态 或 全部   — 完整状态总览
/pool 或 水位 或 池       — 池水位 vs 阈值
/register 或 注册         — 注册历史与成功率
/mint 或 铸造             — 铸造历史(数量/新增/独立用户)
/refresh 或 刷新          — 凭据刷新状态
/reauth 或 重授权         — 待重授权与守护服务
/api 或 调用              — API 请求成功率(近 1d)
/balance 或 余额          — LuckMail / YesCaptcha 余额
/nodes 或 节点            — egress 节点健康与出口 IP
/services 或 服务         — systemd 单元状态
/alerts 或 告警           — 告警通道配置
/help 或 帮助             — 本帮助

示例:/status  /api  /水位
"""

# 命令/关键词 → 板块;首词匹配,忽略大小写与前导 /
CMD_MAP = {
    "status": "all", "all": "all", "s": "all", "总览": "all", "状态": "all", "全部": "all",
    "pool": "pool", "p": "pool", "水位": "pool", "池": "pool",
    "register": "register", "reg": "register", "注册": "register",
    "mint": "mint", "铸造": "mint",
    "refresh": "refresh", "rf": "refresh", "刷新": "refresh",
    "reauth": "reauth", "重授权": "reauth", "重铸": "reauth",
    "api": "api", "usage": "api", "调用": "api",
    "balance": "balance", "bal": "balance", "余额": "balance",
    "nodes": "nodes", "node": "nodes", "节点": "nodes",
    "services": "services", "svc": "services", "服务": "services",
    "alerts": "alerts", "告警": "alerts",
    "help": "help", "start": "help", "?": "help", "帮助": "help",
}


def tg_api(token, method, params=None, timeout=90):
    url = API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def handle_text(text):
    """消息文本 → 板块名;无法识别 → help"""
    raw = (text or "").strip()
    if not raw:
        return "help"
    first = raw.lstrip("/").split()[0].lower()
    return CMD_MAP.get(first, "help")


def split_chunks(text, size=MAX_CHUNK):
    """按行切块,避免 Telegram 4096 上限;单行超长则硬切"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def make_reply(section):
    from status import full_text
    return full_text(section, days=1)


def process_update(up, token, owner):
    """处理单条 update,返回 (handled, summary)"""
    msg = up.get("message") or {}
    cid = str((msg.get("chat") or {}).get("id", ""))
    text = msg.get("text") or ""
    if cid != owner:
        print(f"[TG-BOT] 忽略来自 chat_id={cid} 的消息", flush=True)
        return True, "ignored"
    if not text:
        return True, "no-text"
    section = handle_text(text)
    reply = HELP if section == "help" else None
    if reply is None:
        try:
            reply = make_reply(section)
        except Exception as e:
            reply = f"查询失败: {e}"
    for chunk in split_chunks(reply):
        try:
            tg_api(token, "sendMessage",
                   {"chat_id": cid, "text": chunk, "disable_web_page_preview": "true"})
        except Exception as e:
            print(f"[TG-BOT] 发送失败: {e}", flush=True)
    print(f"[TG-BOT] '{text.strip()[:24]}' → {section}", flush=True)
    return True, section


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not owner:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未配置,机器人无法启动", flush=True)
        return 1
    print(f"[TG-BOT] 启动,长轮询中(仅响应 chat_id={owner})", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT,
                      "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            updates = tg_api(token, "getUpdates", params, timeout=POLL_TIMEOUT + 20)
        except Exception as e:
            print(f"[TG-BOT] getUpdates 失败({e}),5s 后重试", flush=True)
            time.sleep(5)
            continue
        for up in updates.get("result", []):
            offset = up["update_id"] + 1
            try:
                process_update(up, token, owner)
            except Exception as e:
                print(f"[TG-BOT] update 处理异常: {e}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
