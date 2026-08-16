"""
Grok 全自动注册 — 免费版 v6（2026-08-16 重构）
─────────────────────────────
- 邮箱：EmailService（默认 luckmail 买断 ms_imap，token 直查收件箱）
- 发码：浏览器表单原生 fill+点击提交（gRPC 已被 x.ai 封杀，表单是唯一有效通道）
- 验证码：邮件 subject 提取（"SpaceXAI confirmation code: XXX-XXX"，平台字段误提取）
- Turnstile：YesCaptcha（与等码并行解；表单提交被门控且注入无效）
- 注册：浏览器内 fetch 直 POST（next-action/state_tree），SSO 从响应提取
- Clash IP 轮换：每号前切换代理节点（x.ai 单 IP 发码限流 ~3-5 次/小时）
- 成本：~0.024/号（邮箱 0.02 + YesCaptcha ~0.004）
- 输出：SSO + email:password:sso → keys/
"""
import os, re, sys, json, time, random, string, struct, urllib.parse, argparse, subprocess
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from curl_cffi import requests as cf_req
import requests  # 标准 requests 用于 SSO 跳转，兼容 auth.grokipedia.com 等域名

# ── 配置 ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_URL = "https://accounts.x.ai"
FALLBACK_SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
from config import BROWSER_UA as UA
from dotenv import load_dotenv
load_dotenv()

from config import PROXY  # noqa: E402(代理单一来源,原 4 行重复解析删除)
from config import KEYS_DIR as OUTPUT_DIR

# Clash 轮换器（可选，导入失败则跳过 IP 轮换）
# HAS_CLASH 按 API 可达性判定（与 auto_replenish 一致）：
# 仅 import 成功不算数，无 Clash API 的机器上轮换会空转
try:
    from clash_rotator import (random_switch, switch_region, get_current_ip,
                                health as clash_health, snapshot, restore,
                                list_fast_nodes)
except ImportError:
    HAS_CLASH = False
    print("[!] clash_rotator 未找到，IP 轮换功能禁用")
else:
    HAS_CLASH = bool(clash_health().get("ok"))
    if not HAS_CLASH:
        print("[IP] Clash API 不可达 → IP 轮换已禁用（固定出口直连）")

# ═══════════════════════ 工具函数 ═══════════════════════

def rand_str(length=15):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))

def rand_name():
    n = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + ''.join(random.choice(string.ascii_lowercase) for _ in range(n - 1))


def _display_alive():
    """校验当前 DISPLAY 的 X 服务是否活着(自启 Xvfb 可能已被清理)。"""
    disp = os.environ.get("DISPLAY", "")
    m = re.match(r":(\d+)", disp)
    if not m:
        return True  # 远程显示,无法校验,假定存活
    return os.path.exists(f"/tmp/.X11-unix/X{m.group(1)}")


def _ensure_display():
    """无 DISPLAY 或 DISPLAY 失效时启动 Xvfb(headed 过 CF 必需)。返回 True 有可用显示。"""
    global _XVFB_PROC
    if os.environ.get("DISPLAY") and _display_alive():
        return True
    import shutil
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        print("[Display] ⚠️ 无 DISPLAY 且未找到 Xvfb → 尝试 headless(CF 可能拦截)")
        return False
    disp = os.environ.get("GROK_DISPLAY") or ":99"
    try:
        _XVFB_PROC = subprocess.Popen(
            [xvfb, disp, "-screen", "0", "1400x1000x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 轮询 socket 确认 X 服务真的起来了(reviewer F4:盲等 1.5s 可能撞上 stale socket)
        m = re.match(r":(\d+)", disp)
        sock = f"/tmp/.X11-unix/X{m.group(1)}" if m else None
        for _ in range(10):
            if sock and os.path.exists(sock):
                os.environ["DISPLAY"] = disp
                print(f"[Display] Xvfb 已启动: {disp}")
                return True
            if _XVFB_PROC.poll() is not None:
                break
            time.sleep(0.3)
        print(f"[Display] Xvfb 启动失败(未就绪),尝试 headless")
        return False
    except Exception as e:
        print(f"[Display] Xvfb 启动失败: {e}")
        return False


def _find_chromium():
    """自动探测可用 Chromium: patchright/playwright 缓存或系统安装。"""
    import glob
    home = os.path.expanduser("~")
    candidates = []
    for pat in (
        f"{home}/.cache/patchright/chromium-*/chrome-linux*/chrome",
        f"{home}/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ):
        candidates += glob.glob(pat)
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _new_browser():
    """启动 patchright Chromium(stealth)。返回 (playwright, browser, context, page)。"""
    from patchright.sync_api import sync_playwright
    exe = _find_chromium()
    _ensure_display()  # 无 DISPLAY 时起 Xvfb(headed 过 CF)
    pw = sync_playwright().start()
    launch_kwargs = dict(
        headless=not os.environ.get("DISPLAY"),
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--incognito"],
    )
    if exe:
        launch_kwargs["executable_path"] = exe
        print(f"[Browser] 使用: {exe}")
    else:
        print("[Browser] ⚠️ 未找到 Chromium，将尝试默认路径")
    if PROXY:
        launch_kwargs["proxy"] = {"server": PROXY}
    try:
        browser = pw.chromium.launch(**launch_kwargs)
    except Exception as e:
        print(f"[Browser] 启动失败: {str(e)[:120]}")
        pw.stop()
        raise RuntimeError(f"浏览器启动失败: {e}")
    ctx = browser.new_context(
        viewport={"width": random.randint(1200, 1400), "height": random.randint(800, 1000)},
        locale="en-US",
    )
    page = ctx.new_page()
    page.set_default_timeout(45000)
    # 手动渲染需要 ≥60s 的 evaluate(patchright sync evaluate 不接受 timeout kwarg)
    page.set_default_timeout(80000)
    return pw, browser, ctx, page


def _click_email_option(page):
    """点击邮箱注册选项,触发 Turnstile 渲染。"""
    page.evaluate("""() => {
        const all = document.querySelectorAll('button,[role=button]');
        for (let i = 0; i < all.length; i++) {
            if (!all[i].offsetParent) continue;
            const t = (all[i].innerText || '').trim();
            if (t.indexOf('邮箱') >= 0 || t.indexOf('email') >= 0 || t.indexOf('Email') >= 0) {
                all[i].click(); break;
            }
        }
    }""")


def browser_init():
    """打开注册页 → 获取 Action ID + Site Key + Turnstile Token + State Tree"""
    pw, browser, ctx, page = _new_browser()

    print("[Browser] 打开注册页...")
    try:
        page.goto(f"{SITE_URL}/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[Browser] 页面加载异常: {e}")
    page.wait_for_timeout(4000)

    html = page.content()
    if not html or len(html) < 500:
        _close_browser(pw, browser)
        raise RuntimeError("页面加载失败")

    # --- Site Key ---
    site_key = FALLBACK_SITE_KEY
    m = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
    if m:
        site_key = m.group(1)
    print(f"[Browser] SiteKey: {site_key}")

    # --- Action ID ---
    js_urls = re.findall(r"/_next/static/chunks/[^\"'\s>]+\.js", html)
    action_id = None
    js_sess = cf_req.Session(impersonate="chrome120")
    if PROXY:
        js_sess.proxies = {"http": PROXY, "https": PROXY}
    for js_path in js_urls:
        url = js_path if js_path.startswith("http") else f"{SITE_URL}{js_path}"
        try:
            js = js_sess.get(url, timeout=15).text
            m = re.search(r'release[:\s]*["\']([a-fA-F0-9]{40})["\']', js)
            if not m:
                m = re.search(r'7f[a-fA-F0-9]{40}', js)
            if m:
                action_id = m.group(1) if m.lastindex else m.group(0)
                print(f"[Browser] ActionID: {action_id}")
                break
        except Exception:
            continue
    if not action_id:
        _close_browser(pw, browser)
        raise RuntimeError("未找到 Action ID，无法注册")

    # --- State Tree ---
    state_tree = ""
    m = re.search(r'next-router-state-tree":"([^"]+)"', html)
    if m:
        state_tree = m.group(1)

    # --- 点击邮箱注册选项(真实 Turnstile 在 solve_turnstile 提交邮箱后获取) ---
    print("[Browser] 点击邮箱注册选项...")
    _click_email_option(page)
    page.wait_for_timeout(2000)

    return {
        "site_key": site_key,
        "action_id": action_id,
        "state_tree": state_tree,
        "ts_token": "",
        "page": page,
        "_pw": pw,
        "_browser": browser,
        "_ctx": ctx,
    }


def _close_browser(pw=None, browser=None):
    """关闭浏览器(patchright 无 quit,需显式关 browser/playwright)与自启的 Xvfb。"""
    try:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
        if _XVFB_PROC:
            try:
                _XVFB_PROC.terminate()
            except Exception:
                pass
    except Exception:
        pass


def close_browser_cfg(cfg):
    """关闭 cfg 里的浏览器资源(main finally 用)。"""
    _close_browser(cfg.get("_pw"), cfg.get("_browser"))


# ═══════════════════════ 注册流程 ═══════════════════════

def _page_fetch(page, url, method="POST", body=None, headers=None):
    """在页面上下文内 fetch(同源 + 浏览器指纹 → 稳定过 CF,避免 curl_cffi 间歇 403)。

    返回 (status, text)。
    """
    import base64 as _b64
    body_b64 = None
    if isinstance(body, bytes):
        body_b64 = _b64.b64encode(body).decode()
        body = None
    result = page.evaluate("""async ([url, method, body, bodyB64, extraHeaders]) => {
        const opts = { method, headers: Object.assign({ 'accept': '*/*' }, extraHeaders || {}) };
        if (bodyB64) {
            const bin = atob(bodyB64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            opts.body = bytes;
        } else if (body !== null && body !== undefined) {
            opts.body = body;
        }
        const r = await fetch(url, opts);
        const text = await r.text();
        return { status: r.status, text };
    }""", [url, method, body, body_b64, headers or {}])
    return int(result.get("status") or 0), str(result.get("text") or "")


def _extract_code_from_text(text):
    """从任意文本提取 x.ai 验证码(XXX-XXX / XXXXXX 数字字母混合)。"""
    import re as _re
    m = _re.search(r"code[:\\s]*([A-Z0-9]{3})-?([A-Z0-9]{3})", text)
    if m:
        return m.group(1) + m.group(2)
    return None


def _fetch_code_from_subject(token_like, mail_service=None):
    """从邮件 subject 提取验证码,按 provider 分派(reviewer F2)。

    2026-08-16 实测:平台 verification_code 字段对当前 x.ai 邮件
    ("SpaceXAI confirmation code: XXX-XXX")提取错误,subject 里的码才可靠。
    """
    try:
        provider = str(token_like.get("provider") or "").lower()
        client = token_like.get("client")
        tok = token_like.get("token")
        if not client or not tok:
            return None
        if provider == "luckmail_order":
            # 订单模式:get_order_code 的 mail_subject
            code = client.client.user.get_order_code(tok)
            return _extract_code_from_text(
                str(getattr(code, "mail_subject", "") or "")
                + " " + str(getattr(code, "mail_body_html", "") or ""))
        if provider == "luckmail":
            # 买断模式:token 直查收件箱
            mails = client.client.user.get_token_mails(tok)
            items = list(getattr(mails, "mails", []) or [])
            if items:
                return _extract_code_from_text(str(getattr(items[0], "subject", "") or ""))
            return None
        # 其他 provider:走 EmailService fetch + 同一正则
        try:
            svc = mail_service
            content = svc.fetch_first_email(token_like) if svc else None
            return _extract_code_from_text(str(content or ""))
        except Exception:
            return None
    except Exception as e:
        print(f"[Mail] subject 提取失败: {e}")
        return None


def _form_submit_email(page, email):
    """playwright 原生填邮箱 + 点击提交 → 触发 x.ai 发送验证码(实测唯一有效通道)。

    返回 True 表示已提交(页面进入 Verify your email 状态)。"""
    try:
        page.goto(f"{SITE_URL}/sign-up?redirect=grok-com", wait_until="load", timeout=45000)
    except Exception as e:
        print(f"[Browser] 刷新页面异常: {e}")
    page.wait_for_timeout(3000)
    _click_email_option(page)
    page.wait_for_timeout(2000)
    inputs = page.query_selector_all("input")
    target = None
    for inp in inputs:
        try:
            if inp.is_visible():
                target = inp
                break
        except Exception:
            continue
    if not target:
        print("[Browser] ⚠️ 未找到可见输入框")
        return False
    target.fill(email)
    page.wait_for_timeout(800)
    for btn in page.query_selector_all("button"):
        txt = (btn.inner_text() or "").strip()
        if txt in ("Sign up", "注册", "继续", "Continue"):
            btn.click()
            break
    page.wait_for_timeout(2500)
    # 确认进入 Verify your email 状态(或至少页面有变化)
    body = page.evaluate("() => (document.body.innerText || '').slice(0, 200)")
    if "Verify your email" in body or "验证" in body or "code" in body.lower():
        print("[Browser] 已进入验证码等待状态")
        return True
    print(f"[Browser] 页面状态: {body[:80]!r}")
    return True  # 不阻塞;发码与否由收件箱轮询判断


def _create_mail():
    """阶段1:创建邮箱(EmailService 回退链)。返回 (token_like, email) 或 (None, None)。"""
    print("[Mail] 创建邮箱(回退链)...")
    try:
        from email_service import EmailService
        provider = os.getenv("EMAIL_PROVIDER") or "luckmail"
        _mail_service = EmailService(provider=provider)
        token_like, email = _mail_service.create_email()
        if not email:
            raise RuntimeError("create_email 返回空")
    except Exception as e:
        print(f"[Mail] 邮箱创建失败: {e}")
        return None, None, None
    print(f"[Mail] {email}")
    return _mail_service, token_like, email


def _send_code_via_form(page, email):
    """阶段2:表单提交邮箱 → x.ai 发码。

    2026-08-16 实测:gRPC CreateEmailValidationCode 已被 x.ai 静默封杀
    (HTTP 200/grpc-status:0 但邮件不送达);表单提交(playwright 原生 fill+点击)
    是唯一有效通道,页面进入 "Verify your email" 状态。
    """
    print(f"[{email}] 提交邮箱表单(触发发码)...")
    if not _form_submit_email(page, email):
        print(f"[{email}] 表单提交失败")
        return False
    print(f"[{email}] 验证码已发送(等待送达)")
    return True


def _wait_for_code(token_like, mail_service, email):
    """阶段3:轮询验证码(75s 上限)。

    码实测 5-60s 到达;超 75s 基本=IP 限流(永远不到)。缩短等待减少
    限流节点上的时间浪费与邮箱烧损(每次尝试=0.02)。
    """
    print(f"[{email}] 等待验证码(75s 上限)...")
    for _ in range(15):  # 15 x 5s = 75s
        time.sleep(5)
        code = _fetch_code_from_subject(token_like, mail_service)
        if code:
            print(f"[{email}] 验证码: {code}")
            return code
    print(f"[{email}] 未收到验证码（超时,可能 IP 限流）")
    return None


def _solve_turnstile_parallel(cfg):
    """阶段4a:启动 YesCaptcha 后台线程解 Turnstile。

    与等码并行(reviewer F5:串行最坏 363s > 子进程 timeout 300s)。
    返回 dict:{"join": 线程 join 函数, "result": 结果字典}。
    """
    import threading as _th
    from YesCaptcha_service import TurnstileService
    _ts_svc = TurnstileService()
    _task = _ts_svc.create_task(SITE_URL, cfg["site_key"])
    print(f"[YesCaptcha] 任务: {_task[:20]}... (并行解)")
    ts_result = {}

    def _solve():
        try:
            ts_result["token"] = _ts_svc.get_response(_task, max_retries=20,
                                                      initial_delay=3, retry_delay=3)
        except Exception as e:
            ts_result["error"] = str(e)

    _ts_thread = _th.Thread(target=_solve, daemon=True)
    _ts_thread.start()
    return {"join": lambda: _ts_thread.join(timeout=90), "result": ts_result}


def _wait_turnstile_result(ts_handle, email):
    """阶段4b:等 YesCaptcha 线程结果,返回 token 或 None。"""
    ts_handle["join"]()
    ts_result = ts_handle["result"]
    ts_token = ts_result.get("token") or ""
    if ts_result.get("error"):
        print(f"[{email}] YesCaptcha 异常: {ts_result['error']}")
        return None
    if not ts_token or len(ts_token) < 50:
        print(f"[{email}] YesCaptcha 未返回有效 token")
        return None
    print(f"[{email}] Turnstile 已获取({len(ts_token)} chars)")
    return ts_token


def _post_signup(page, cfg, email, code, ts_token, password, first, last):
    """阶段5:直 POST /sign-up(浏览器内 fetch,next-action/state-tree)。返回 (status, resp_text)。"""
    print(f"[{email}] 提交注册...")
    payload = json.dumps([{
        "emailValidationCode": code,
        "createUserAndSessionRequest": {
            "email": email,
            "givenName": first,
            "familyName": last,
            "clearTextPassword": password,
            "tosAcceptedVersion": "$undefined",
        },
        "turnstileToken": ts_token,
        "promptOnDuplicateEmail": True,
    }])
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "origin": SITE_URL,
        "referer": f"{SITE_URL}/sign-up",
        "next-router-state-tree": cfg["state_tree"],
        "next-action": cfg["action_id"],
    }
    try:
        r_status, resp_text = _page_fetch(page, f"{SITE_URL}/sign-up", body=payload, headers=headers)
        print(f"[{email}] POST 状态: {r_status}")
        return r_status, resp_text
    except Exception as e:
        print(f"[{email}] POST 异常: {e}")
        return 0, ""


def _extract_sso(resp_text, cfg, email):
    """阶段6:从 POST 响应提取 sso cookie。返回 sso 或 None。"""
    sso_url = None
    for pat in [
        r'(https://[^"\s]+set-cookie\?q=[^:"\s]+)1:',
        r'(https://[^"\s]+set-cookie\?q=[^"\s]+)',
        r'https://[^"\s]*set-cookie[^"\s]*',
    ]:
        m = re.search(pat, resp_text)
        if m:
            sso_url = m.group(0).rstrip("1:")
            break

    if not sso_url:
        print(f"[{email}] 响应中无 SSO URL，前300字符:")
        print(f"  {resp_text[:300]}")
        return None

    sso_url = re.sub(r'[:\d]*$', '', sso_url) if sso_url.endswith(('1:', '2:', '3:')) else sso_url
    print(f"[{email}] SSO URL: {sso_url[:100]}...")

    sso = None
    try:
        rs = requests.Session()
        if PROXY:
            rs.proxies = {"http": PROXY, "https": PROXY}
        rs.get(sso_url, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
        # 遍历 cookie jar:redirect 链可能设多个同名 sso cookie
        for ck in rs.cookies:
            if ck.name == "sso" and ck.value:
                sso = ck.value
                break
    except Exception as e:
        print(f"[{email}] SSO 请求异常: {e}，回退浏览器导航...")
        try:
            np = cfg["_ctx"].new_page()
            np.goto(sso_url, wait_until="domcontentloaded", timeout=20000)
            np.wait_for_timeout(3000)
            for ck in cfg["_ctx"].cookies():
                if ck["name"] == "sso":
                    sso = ck["value"]
                    break
            np.close()
        except Exception as e2:
            print(f"[{email}] SSO 浏览器回退也失败: {e2}")

    if not sso:
        print(f"[{email}] 无 SSO cookie")
        return None
    return sso


def _save_account(email, password, sso):
    """阶段7:SSO 落盘 keys/(grok.txt + accounts.txt)。"""
    print(f"[{email}] ✅ SSO: {sso[:30]}...")
    with open(os.path.join(OUTPUT_DIR, "grok.txt"), "a", encoding="utf-8") as f:
        f.write(sso + "\n")
    with open(os.path.join(OUTPUT_DIR, "accounts.txt"), "a", encoding="utf-8") as f:
        f.write(f"{email}:{password}:{sso}\n")


def register_one(cfg):
    """注册单个账号 → (email, password, sso) 或 None(阶段编排,各阶段独立可测)。"""
    mail_service, token_like, email = _create_mail()
    if not email:
        return None
    page = cfg["page"]

    if not _send_code_via_form(page, email):
        return None

    code = _wait_for_code(token_like, mail_service, email)
    if not code:
        return None

    password = rand_str(14) + "Aa1!"
    first = rand_name()
    last = rand_name()

    ts_handle = _solve_turnstile_parallel(cfg)
    ts_token = _wait_turnstile_result(ts_handle, email)
    if not ts_token:
        return None

    r_status, resp_text = _post_signup(page, cfg, email, code, ts_token, password, first, last)
    if r_status != 200:
        print(f"[{email}] 注册失败: {resp_text[:300]}")
        return None

    sso = _extract_sso(resp_text, cfg, email)
    if not sso:
        return None

    _save_account(email, password, sso)
    return (email, password, sso)


def main():
    parser = argparse.ArgumentParser(description="Grok 全自动注册")
    parser.add_argument("--count", type=int, default=3, help="注册数量（默认 3）")
    parser.add_argument("--no-rotate", action="store_true", help="禁用 IP 轮换")
    parser.add_argument("--rotate-interval", type=int, default=1,
                        help="每注册 N 个后切换 IP（默认 1，即每次切换）")
    parser.add_argument("--min-delay", type=int, default=8, help="注册间隔最小值（秒，默认 8）")
    parser.add_argument("--max-delay", type=int, default=25, help="注册间隔最大值（秒，默认 25）")
    parser.add_argument("--rotate-region", action="store_true", help="切换不同区域节点（而非随机节点）")
    args = parser.parse_args()

    print("=" * 55)
    print(f"Grok 注册 · 免费版 v6 (表单发码 + YesCaptcha + Clash)")
    print(f"数量: {args.count}  IP轮换: {'✅' if not args.no_rotate and HAS_CLASH else '❌'}")
    print("=" * 55)

    # ── Clash: 快照当前节点（注册完恢复） ──
    original_node = None
    if not args.no_rotate and HAS_CLASH:
        try:
            original_node = snapshot()
            h = clash_health()
            # 显示低延迟节点数
            fast, slow = list_fast_nodes()
            print(f"[Clash] 快照: {h['current_node'][:40]}")
            print(f"[Clash] 出口 IP: {h['current_ip']}  ({h['region']})")
            print(f"[Clash] 低延迟节点: {len(fast)}  慢/断线: {len(slow)}")
            if slow:
                for n, reason in slow[:3]:
                    print(f"[Clash]   ⚠ {n[:35]} — {reason}")
        except Exception as e:
            print(f"[Clash] ⚠️ 检查失败: {e}")

    # 1. 浏览器初始化（获取 Turnstile token + Action ID 等），失败重试
    cfg = None
    for retry in range(3):
        try:
            cfg = browser_init()
            break
        except RuntimeError as e:
            print(f"\n[!] 浏览器初始化失败 (尝试 {retry+1}/3): {e}")
            if retry < 2 and HAS_CLASH and not args.no_rotate:
                try:
                    # 换个区域重试 Turnstile
                    random_switch()
                    print(f"[Clash] 切换区域后重试...")
                except Exception:
                    pass
            time.sleep(5)
    if not cfg:
        print("[!] 浏览器初始化失败，放弃")
        return

    # 2. 注册循环
    success = 0
    fail = 0
    t0 = time.time()

    # 使用过的区域追踪（避免重复用同一区域）
    used_regions = set()

    try:
        for i in range(args.count):
            print(f"\n{'─'*40}")
            print(f"第 {i+1}/{args.count} 次注册")
            print(f"{'─'*40}")

            # ── IP 轮换 ──
            if not args.no_rotate and HAS_CLASH and i > 0 and i % args.rotate_interval == 0:
                try:
                    if args.rotate_region:
                        switch_region(exclude_regions=used_regions)
                    else:
                        random_switch()
                    new_ip = get_current_ip()
                    if new_ip:
                        print(f"  [IP] 新出口 IP: {new_ip}")
                except Exception as e:
                    print(f"  [IP] ⚠️ 切换失败: {e}，继续使用当前 IP")

            try:
                result = register_one(cfg)
                if result:
                    success += 1
                    avg = (time.time() - t0) / success
                    print(f"  ✅ 成功={success} 失败={fail} 均速={avg:.0f}s/个")
                else:
                    fail += 1
                    print(f"  ❌ 失败 成功={success} 失败={fail}")
            except KeyboardInterrupt:
                break
            except Exception as e:
                fail += 1
                print(f"[!] 异常: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5)

            # ── 随机间隔（最后一个不需要等待） ──
            if i < args.count - 1:
                delay = random.uniform(args.min_delay, args.max_delay)
                print(f"  ⏳ 等待 {delay:.1f}s...")
                time.sleep(delay)
    finally:
        if cfg and cfg.get("page"):
            close_browser_cfg(cfg)
        # ── 恢复原始节点 ──
        if original_node and HAS_CLASH:
            try:
                restore(original_node)
            except Exception as e:
                print(f"[Clash] ⚠️ 恢复节点失败: {e}")

    elapsed = time.time() - t0
    print(f"\n{'='*55}")
    print(f"结束。成功={success} 失败={fail} 耗时={elapsed:.0f}s")
    if success > 0:
        print(f"SSO 已保存至: {OUTPUT_DIR}")
    print(f"{'='*55}")
    # 全失败 → 非零退出:auto_replenish 的 register_one_free 靠 returncode
    # 区分成败(否则 returncode 0 被当成功,失败重试永不触发)
    if success == 0 and fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

