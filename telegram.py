#!/usr/bin/env python3
"""Telegram 统一模块:查询机器人(命令/回复键盘)+ 发送通道(推送/回复共用)。

职责:
  - run_query_bot()   常驻长轮询:命令/关键词 + 回复键盘 → 回复状态(仅本人可查)
  - send_message()    统一发送(HTML/纯文本,按行分块,防 Telegram 4096 上限)
  - render_html()     把 status.py 的 CLI 文本渲染成紧凑 HTML(窄窗口友好)

两个入口共用本模块:
  - tg_bot.py         查询机器人(薄壳,deploy/vps-grok-tg-bot.service)
  - alert.py          事件推送(send_telegram 委托 send_message)

命令(首词匹配,中英文):
  /status 状态 全部 /pool 水位 /register 注册 /mint 铸造 /refresh 刷新
  /reauth 重授权 /api 调用 /balance 余额 /nodes 节点 /services 服务
  /alerts 告警 /help 帮助
"""
import html as _html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 50
MAX_CHUNK = 3500  # 单条消息上限 4096,留余量(HTML 转义后可能变长)

# ── 回复键盘(点击即发送命令,免敲字) ──

HELP = """<b>grok-register 状态查询</b>

输入框上方有常驻按钮(点击即发送命令);也可直接发:

/status 或 状态 全部    → 完整状态总览
/pool 或 水位           → 池水位 vs 阈值
/register 或 注册       → 注册历史与成功率
/mint 或 铸造           → 铸造历史
/refresh 或 刷新        → 凭据刷新状态
/reauth 或 重授权       → 待重授权与守护
/api 或 调用            → API 成功率
/balance 或 余额        → 余额
/nodes 或 节点          → 出口节点健康
/services 或 服务       → systemd 状态
/help 或 帮助           → 本帮助

查询纯只读,不会触发任何注册/铸造/刷新动作。"""

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

# 回复键盘(常驻输入区上方):点击 = 直接发送对应命令文本,由普通消息路径处理
KB_REPLY = {
    "keyboard": [
        [{"text": "📊 /status"}, {"text": "🌊 /pool"}, {"text": "🔄 /refresh"}],
        [{"text": "📝 /register"}, {"text": "🎨 /mint"}, {"text": "⚡ /api"}],
        [{"text": "💰 /balance"}, {"text": "🖥 /nodes"}, {"text": "⚙️ /services"}],
        [{"text": "🔁 /reauth"}, {"text": "🔔 /alerts"}, {"text": "❓ /help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "点按钮,或输入 / 命令",
}

# 每条回复底部的提示脚注(命令本体在回复键盘按钮上可见)
CMD_FOOTER = "\n\n—\n点下方按钮直接发送命令;输入框打 <code>/</code> 可弹出全部说明"

# Telegram 原生命令菜单(输入 / 时弹出的提示):command 仅限小写字母/数字/下划线
BOT_COMMANDS = [
    {"command": "status", "description": "完整状态总览"},
    {"command": "pool", "description": "池水位 vs 阈值"},
    {"command": "register", "description": "注册历史与成功率"},
    {"command": "mint", "description": "铸造历史"},
    {"command": "refresh", "description": "凭据刷新状态"},
    {"command": "reauth", "description": "待重授权与守护"},
    {"command": "api", "description": "API 请求成功率"},
    {"command": "balance", "description": "LuckMail/YesCaptcha 余额"},
    {"command": "nodes", "description": "出口节点健康"},
    {"command": "services", "description": "systemd 服务状态"},
    {"command": "alerts", "description": "告警通道配置"},
    {"command": "help", "description": "命令与按钮说明"},
]


def set_my_commands(token):
    """注册命令菜单:用户在输入框打 / 即可看到全部命令与说明。幂等,启动时调用一次。"""
    try:
        tg_api(token, "setMyCommands",
               {"commands": json.dumps(BOT_COMMANDS)})
        print("[TG-BOT] 命令菜单已注册(setMyCommands)", flush=True)
        return True
    except Exception as e:
        print(f"[TG-BOT] 命令菜单注册失败: {e}", flush=True)
        return False


def tg_api(token, method, params=None, timeout=90):
    url = API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def split_chunks(text, size=MAX_CHUNK):
    """按行分块(HTML 标签不跨行,安全);单行超长硬切"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:size])
            line = line[size:]
        if cur and len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_message(text, parse_mode=None, chat_id=None, token=None,
                 reply_markup=None):
    """统一发送:默认发给 TELEGRAM_CHAT_ID;分块发送避免 4096 上限。
    返回成功块数;未配置时返回 0 并打印警告。"""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = str(chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not (token and chat_id):
        print("[TG] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未配置,跳过发送", flush=True)
        return 0
    sent = 0
    for chunk in split_chunks(text):
        params = {"chat_id": chat_id, "text": chunk,
                  "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        try:
            tg_api(token, "sendMessage", params)
            sent += 1
        except Exception as e:
            print(f"[TG] 发送失败: {e}", flush=True)
    return sent


def render_html(text):
    """status.py CLI 文本 → 紧凑 HTML:标题加粗、| 换行、服务状态转 emoji、转义"""
    out = []
    for line in text.split("\n"):
        m = re.match(r"^── (.+?) ─+$", line)
        if m:
            out.append("<b>▍" + m.group(1) + "</b>")
            continue
        if line.startswith("grok-register 状态总览"):
            line = re.sub(r"\s+\(GROK2API_DB=.*\)$", "", line)  # 头部去掉 DB 路径噪声
            line = "📊 " + line
        line = line.replace("●", "🟢").replace("○", "⚪").replace("✗", "🔴")
        line = line.replace(" | ", "\n  ")   # 窄窗口友好:长行在 | 处折行
        out.append(_html.escape(line))
    return "\n".join(out)


def handle_text(text):
    raw = (text or "").strip()
    if not raw:
        return "help"
    # 扫描所有 token:按钮标签带 emoji 前缀(如 "📊 /status"),首个 token 可能不是命令
    for tok in raw.split():
        cmd = CMD_MAP.get(tok.lstrip("/").lower())
        if cmd:
            return cmd
    return "help"


def make_reply(section):
    from status import full_text
    return render_html(full_text(section, days=1))


def process_update(up, token, owner):
    """单条 update:message(命令/关键词;回复键盘按钮点击 = 发送命令文本,走同一路径)"""
    t0 = time.time()
    msg = up.get("message") or {}
    cid = str((msg.get("chat") or {}).get("id", ""))
    text = msg.get("text") or ""
    if cid != owner:
        print(f"[TG-BOT] 忽略来自 chat_id={cid} 的消息", flush=True)
        return "ignored"
    if not text:
        return "no-text"
    section = handle_text(text)
    reply = HELP if section == "help" else None
    if reply is None:
        try:
            reply = make_reply(section) + CMD_FOOTER
        except Exception as e:
            reply = f"查询失败: {e}"
    send_message(reply, parse_mode="HTML", chat_id=cid, token=token,
                 reply_markup=KB_REPLY)
    print(f"[TG-BOT] '{text.strip()[:24]}' → {section} (处理 {time.time()-t0:.1f}s)", flush=True)
    return section


def run_query_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not owner:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未配置,机器人无法启动", flush=True)
        return 1
    set_my_commands(token)
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
    sys.exit(run_query_bot())
