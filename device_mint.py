"""
SSO → OAuth Device Flow 铸造模块 (2026-08-06)
替换被 CF 拦截的 PKCE 方案 (sso_to_cpa.py)。

原理: x.ai OAuth Device Authorization Grant
- 关键: scope 必须为 7 个, 不能加 conversations/workspaces (该 client 未授权, 加了 Access denied)
- 授权: 有头 Chrome 注入 SSO cookie → 打开授权页自动点"继续/允许" → 轮询 token

接口: sso_to_device(sso, email) -> cpa_data (与 sso_to_cpa.py 返回结构一致)
"""
import json, sys, time, base64, asyncio, urllib.request, urllib.error, urllib.parse, os, re, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from xai_oauth import CLIENT_ID
SCOPE = "openid profile email offline_access grok-cli:access api:access"
from dotenv import load_dotenv
load_dotenv()

from config import PROXY  # noqa: E402(代理单一来源,原 4 行重复解析删除)


def _http_json(url, method="GET", form=None, timeout=40):
    proxy_handler = (urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
                     if PROXY else urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(proxy_handler)
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "grok-register-cpa/1.0")
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def _display_alive():
    disp = os.environ.get("DISPLAY", "")
    m = re.match(r":(\d+)", disp)
    if not m:
        return bool(disp)  # 远程显示,假定存活
    return os.path.exists(f"/tmp/.X11-unix/X{m.group(1)}")


def _ensure_display():
    """无 DISPLAY 或 DISPLAY 失效时启动 Xvfb(headed 过 CF 必需,与 grok_free 同款)。"""
    if _display_alive():
        return True
    import shutil
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        print("  [DEVICE] ⚠️ 无 DISPLAY 且无 Xvfb,尝试 headless(CF 可能拦截)")
        return False
    disp = os.environ.get("GROK_DISPLAY") or ":99"
    try:
        subprocess.Popen([xvfb, disp, "-screen", "0", "1400x1000x24", "-ac"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        m = re.match(r":(\d+)", disp)
        sock = f"/tmp/.X11-unix/X{m.group(1)}" if m else None
        for _ in range(10):
            if sock and os.path.exists(sock):
                os.environ["DISPLAY"] = disp
                print(f"  [DEVICE] Xvfb 已启动: {disp}")
                return True
            time.sleep(0.3)
        print("  [DEVICE] Xvfb 启动失败(未就绪),尝试 headless")
        return False
    except Exception as e:
        print(f"  [DEVICE] Xvfb 启动失败: {e}")
        return False


async def _browser_authorize(vuc, sso_jwt, headless=False):
    """注入 SSO cookie, 自动点授权按钮. headless=True 供 VPS 无显示器环境"""
    if not headless and not os.environ.get("DISPLAY"):
        _ensure_display()  # VPS 无显示器 → Xvfb headed(CF 对 headless 拦截)
        headless = not os.environ.get("DISPLAY")
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=headless,
            proxy={"server": PROXY} if PROXY else None,
            args=["--disable-blink-features=AutomationControlled"])
        browser = None
        # 启动链: 系统 Chrome → 完整 Chromium(headless=new, stealth 补丁完整生效)
        # → headless shell(兜底)。CF 对 headless shell 指纹识别率高。
        # 2026-08-16: channel 探测在部分机器找不到 google-chrome(即使已安装),
        # 增加 executable_path 显式回退(VPS: /usr/bin/google-chrome 实测可用)。
        for channel in ("chrome", "chromium", None):
            kw = dict(launch_kwargs)
            if channel:
                kw["channel"] = channel
            try:
                browser = await p.chromium.launch(**kw)
                if channel == "chromium":
                    print("  [DEVICE] 使用完整 Chromium (headless)")
                break
            except Exception as e:
                if channel is None:
                    print(f"  [DEVICE] 浏览器启动失败: {str(e)[:100]}")
                else:
                    print(f"  [DEVICE] 系统 {channel} 不可用 ({str(e)[:60]}), 尝试下一级")
        if browser is None:
            for exe in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                        "/usr/bin/chromium", "/usr/bin/chromium-browser"):
                if not os.path.exists(exe):
                    continue
                try:
                    browser = await p.chromium.launch(
                        **{**launch_kwargs, "executable_path": exe})
                    print(f"  [DEVICE] 使用显式路径 {exe} (headless)")
                    break
                except Exception as e:
                    print(f"  [DEVICE] {exe} 启动失败: {str(e)[:80]}")
        if browser is None:
            return False
        ctx = await browser.new_context(viewport={"width": 1000, "height": 700})
        await ctx.add_cookies([{
            "name": "sso", "value": sso_jwt, "domain": ".x.ai", "path": "/",
        }])
        page = await ctx.new_page()
        try:
            await page.goto(vuc, timeout=40000, wait_until="domcontentloaded")
            # CF 挑战: 等待自动通过(stealth 完整 Chromium 下可能直接放行)
            for _ in range(30):
                title = await page.title()
                if "Attention Required" in title or "Just a moment" in title:
                    await asyncio.sleep(2)
                    continue
                break
            # Turnstile 自解: 页面嵌入验证码组件时等待自然通过
            # (干净 IP + 隐身浏览器下隐形模式自动解, 免费; 无组件则立即跳过)
            for _ in range(30):
                has_ts = await page.evaluate(
                    """() => !!document.querySelector(
                        "[name=cf-turnstile-response], iframe[src*='turnstile'], iframe[src*='challenges']"
                    )"""
                )
                if not has_ts:
                    break
                tok = await page.evaluate(
                    'document.querySelector("[name=cf-turnstile-response]")?.value || ""'
                )
                if len(tok) > 50:
                    print("  [DEVICE] Turnstile 自然通过")
                    break
                await asyncio.sleep(2)
            for _ in range(20):
                await asyncio.sleep(2)
                if "/device/done" in page.url:
                    return True
                for sel in ["button:has-text('继续')", "button:has-text('Allow')",
                            "button:has-text('允许')", "button:has-text('Continue')",
                            "button[type=submit]"]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            break
                    except Exception:
                        pass
            print(f"  [DEVICE] 授权页未完成 (URL={page.url[:80]} TITLE={await page.title()})")
        except Exception as e:
            print(f"  [DEVICE] 授权页异常: {str(e)[:100]}")
        finally:
            await browser.close()
    return False


def sso_to_device(sso_token, email="", wait_manual=True):
    """Device Flow 铸造: SSO cookie → OAuth AT/RT。
    wait_manual=False 时自动授权失败立即返回(批量模式, 无人手动授权)。
    返回 dict(access_token/refresh_token/expires_in/token_type) 或 None"""
    headless = os.getenv("GROK_HEADLESS", "0").strip().lower() in ("1", "true", "yes")
    try:
        s, p = _http_json("https://auth.x.ai/oauth2/device/code", "POST",
                          {"client_id": CLIENT_ID, "scope": SCOPE})
        if s != 200 or not isinstance(p, dict) or "device_code" not in p:
            print(f"  [DEVICE] device/code 失败 HTTP {s}: {str(p)[:150]}")
            return None
        dc = p["device_code"]
        vuc = p.get("verification_uri_complete")
        ok = False
        try:
            ok = asyncio.run(_browser_authorize(vuc, sso_token, headless=headless))
        except ImportError:
            print("  [DEVICE] patchright 未安装，降级为手动授权")
        except Exception as e:
            print(f"  [DEVICE] 浏览器自动化失败: {str(e)[:100]}，降级为手动授权")
        if not ok:
            if wait_manual:
                print(f"  [DEVICE] 请手动打开以下链接并在 5 分钟内完成授权:")
                print(f"  {vuc}")
                print("  [DEVICE] 等待授权确认...")
            else:
                print("  [DEVICE] 自动授权失败(批量模式跳过手动等待)")
                return None
        deadline = time.time() + (300 if wait_manual else 15)
        interval = max(int(p.get("interval", 5)), 1)
        while time.time() < deadline:
            time.sleep(interval)
            s, t = _http_json("https://auth.x.ai/oauth2/token", "POST", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dc, "client_id": CLIENT_ID})
            if s == 200 and isinstance(t, dict) and t.get("access_token"):
                return {
                    "access_token": t["access_token"],
                    "refresh_token": t.get("refresh_token", ""),
                    "expires_in": int(t.get("expires_in") or 21600),
                    "token_type": t.get("token_type", "Bearer"),
                }
            err = t.get("error") if isinstance(t, dict) else None
            if err in ("access_denied", "expired_token"):
                print(f"  [DEVICE] token 失败: {err}: {t.get('error_description', '')[:80]}")
                return None
            if err == "slow_down":
                interval += 5
        print("  [DEVICE] 轮询超时")
        return None
    except Exception as e:
        print(f"  [DEVICE] 异常: {str(e)[:120]}")
        return None


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="SSO → OAuth Device Flow 铸造（批量）")
    p.add_argument("--all", action="store_true",
                   help="转换 keys/accounts.txt 中所有未铸造账号（跳过 auths/ 已有）")
    p.add_argument("--email", default=None, help="只铸造指定邮箱（默认取第一个）")
    p.add_argument("--check-sso", type=int, default=0,
                   help="用前 N 个 SSO 各铸造一次并打印 sub（验证账号独立性，不保存）")
    args = p.parse_args()

    sso_map = {}
    from config import AUTH_DIR, KEYS_DIR
    accounts_path = os.path.join(KEYS_DIR, "accounts.txt")
    if not os.path.isfile(accounts_path):
        print(f"keys/accounts.txt 不存在: {accounts_path}")
        sys.exit(1)
    for l in open(accounts_path, encoding="utf-8-sig").read().splitlines():
        parts = l.split(":")
        if len(parts) >= 3:
            sso_map[parts[0]] = ":".join(parts[2:])
    if not sso_map:
        print("keys/accounts.txt 中无账号")
        sys.exit(1)

    if args.check_sso:
        import base64 as _b64

        def _sub(tok):
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(_b64.urlsafe_b64decode(payload)).get("sub")

        for email in list(sso_map)[:args.check_sso]:
            print(f"\n验证铸造: {email}")
            r = sso_to_device(sso_map[email], email)
            if r and r.get("access_token"):
                print(f"  sub={_sub(r['access_token'])}")
            else:
                print(f"  {email} 铸造失败")
        sys.exit(0)

    if args.all:
        from auth_store import save_auth
        os.makedirs(AUTH_DIR, exist_ok=True)
        existing = set()
        for fn in os.listdir(AUTH_DIR):
            if fn.startswith("xai-") and fn.endswith(".json"):
                try:
                    with open(os.path.join(AUTH_DIR, fn), encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("email"): existing.add(d["email"])
                except Exception: pass
        pending = [e for e in sso_map if e not in existing]
        print(f"总账号: {len(sso_map)} 已铸造: {len(existing)} 待铸造: {len(pending)}")
        # 多轮重试: 失败进入下一轮, 直到全部成功或达到 MAX_MINT_ROUNDS
        max_rounds = max(1, int(os.getenv("MAX_MINT_ROUNDS", "3")))
        total_success = 0
        total_pending = len(pending)
        for round_no in range(1, max_rounds + 1):
            if not pending:
                break
            print(f"\n=== 第 {round_no}/{max_rounds} 轮: {len(pending)} 个待铸造 ===")
            round_success, next_pending = 0, []
            for email in pending:
                print(f"\n铸造: {email}")
                result = sso_to_device(sso_map[email], email, wait_manual=False)
                if result:
                    save_auth(email, result)
                    round_success += 1
                else:
                    print(f"  {email} 铸造失败，进入下一轮")
                    next_pending.append(email)
                time.sleep(3)
            total_success += round_success
            print(f"第 {round_no} 轮: {round_success}/{len(pending)} 成功")
            pending = next_pending
            if pending and round_no < max_rounds:
                print(f"剩余 {len(pending)} 个失败，等待 10s 后进入第 {round_no + 1} 轮...")
                time.sleep(10)
        print(f"\n完成: {total_success}/{total_pending} 铸造成功"
              f"{f'，{len(pending)} 个失败(已达最大 {max_rounds} 轮)' if pending else ''}")
    else:
        email = args.email or list(sso_map.keys())[0]
        print(f"测试铸造: {email}")
        result = sso_to_device(sso_map.get(email, ""), email)
        if result:
            print(f"✅ 成功: AT={result['access_token'][:30]}... RT={result['refresh_token'][:20]}...")
        else:
            print("❌ 失败")
