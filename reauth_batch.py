"""批量自动重授权: 对 grok2api 中 reauthRequired 的 Build 账号逐个
SSO → device flow 自动授权 → save_auth → push(force) → 校验 DB active。

- 可断点续跑: 已 active 的自动跳过
- 节点策略: 候选好节点轮换; 失败时浏览器探测刷新好节点列表重试 1 次
- 用法: python reauth_batch.py [最多处理N个]
"""
import os, sys, time, asyncio, sqlite3
_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)
os.chdir(_BASE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from clash_rotator import switch_node, get_current_ip, list_nodes
from device_mint import sso_to_device, _http_json, CLIENT_ID, SCOPE
from auth_store import save_auth
from auto_replenish import push_to_grok2api
from patchright.async_api import async_playwright

DB = os.getenv("GROK2API_DB") or '/root/workspace/grok2api/data/backend.db'
MAX_ACCOUNTS = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
SLEEP_BETWEEN = 5

def get_reauth_emails(limit=None):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT email FROM provider_accounts WHERE provider='grok_build' AND auth_status='reauthRequired' ORDER BY email").fetchall()
    conn.close()
    return [r[0] for r in rows][:limit]

def load_sso_map():
    m = {}
    for line in open('keys/accounts.txt', encoding='utf-8-sig').read().splitlines():
        p = line.split(':')
        if len(p) >= 3:
            m[p[0].lower()] = ':'.join(p[2:])
    return m

def db_active(email):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT auth_status FROM provider_accounts WHERE email=?", (email,)).fetchone()
    conn.close()
    return bool(row and row[0] == 'active')

async def browser_probe_good_nodes(candidates, want=4):
    """浏览器探测哪些节点能过 device 页, 返回通过节点列表"""
    proxy = os.getenv('GROK_PROXY') or 'http://127.0.0.1:7890'
    s, p = _http_json('https://auth.x.ai/oauth2/device/code', 'POST',
                      {'client_id': CLIENT_ID, 'scope': SCOPE})
    if s != 200:
        return []
    vuc = p.get('verification_uri_complete', '')
    good = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False, proxy={'server': proxy},
            args=['--disable-blink-features=AutomationControlled'])
        try:
            for node in candidates:
                if len(good) >= want:
                    break
                switch_node(node)
                ctx = await browser.new_context(viewport={'width': 1000, 'height': 700})
                page = await ctx.new_page()
                try:
                    await page.goto(vuc, timeout=30000, wait_until='domcontentloaded')
                    await asyncio.sleep(7)
                    url = page.url
                    text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 80) : ''")
                    if 'user_code' in url and text and 'Blocked' not in text and 'Just a moment' not in text:
                        good.append(node)
                        print('  [PROBE] %s → 通过' % node[:28], flush=True)
                except Exception:
                    pass
                finally:
                    await ctx.close()
        finally:
            await browser.close()
    return good

def run():
    emails = get_reauth_emails(MAX_ACCOUNTS)
    print('待重授权:', len(emails))
    if not emails:
        print('无待处理账号')
        return
    sso_map = load_sso_map()
    nodes, now = list_nodes()
    # 候选: 家宽 + 原生优先, 剔除硬拦截嫌疑
    good = [n for n in nodes if ('家宽' in n or '原生' in n or '香港01' in n or '新加坡01' in n or '日本01' in n)]

    print('== 浏览器探测好节点 ==', flush=True)
    good = asyncio.run(browser_probe_good_nodes(good, want=6)) or good[:6]
    print('好节点池:', len(good), flush=True)
    if not good:
        print('无可用节点, 中止')
        return

    ok_cnt = fail_cnt = 0
    fails = []
    for i, email in enumerate(emails):
        node = good[i % len(good)]
        sso = sso_map.get(email.lower(), '')
        if not sso:
            print('[%d/%d] SKIP %s (无SSO)' % (i+1, len(emails), email), flush=True)
            fail_cnt += 1
            continue
        if db_active(email):
            print('[%d/%d] SKIP %s (已active)' % (i+1, len(emails), email), flush=True)
            ok_cnt += 1
            continue
        switch_node(node)
        print('[%d/%d] %s @%s' % (i+1, len(emails), email, node[:22]), flush=True)
        t0 = time.time()
        result = sso_to_device(sso, email, wait_manual=False)
        if not result and i >= 0:
            # 刷新节点池重试一次
            print('  首轮失败, 刷新节点重试', flush=True)
            good2 = asyncio.run(browser_probe_good_nodes(nodes, want=3))
            if good2:
                good = good2 + good
                switch_node(good[0])
                result = sso_to_device(sso, email, wait_manual=False)
        if not result:
            print('  FAIL (%.0fs)' % (time.time() - t0), flush=True)
            fail_cnt += 1
            fails.append(email)
            continue
        save_auth(email, result)
        push_to_grok2api([email], force=True)
        time.sleep(2)
        if db_active(email):
            ok_cnt += 1
            print('  ✅ OK (%.0fs)' % (time.time() - t0), flush=True)
        else:
            fail_cnt += 1
            fails.append(email)
            print('  ⚠️ 推送后仍非active', flush=True)
        time.sleep(SLEEP_BETWEEN)
    print('\n== 完成: OK=%d FAIL=%d ==' % (ok_cnt, fail_cnt))
    if fails:
        print('失败列表:')
        for e in fails:
            print('  ', e)


if __name__ == "__main__":
    run()
