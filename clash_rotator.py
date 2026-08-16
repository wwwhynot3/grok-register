"""
Clash 代理节点轮换器 v2
──────────────────────
通过 Clash Verge REST API 切换代理节点，实现 IP 轮换。
用于批量注册时降低风控风险。

关键设计：保护日常使用节点
  - stable_node: 日常流量节点（Claude/ChatGPT 等），轮换时自动排除
  - snapshot/restore: 注册前快照 → 注册后恢复，日常流量不受影响
  - 订阅更新后节点名变化 → 自动 fallback 到相似区域节点

API:
  - 获取节点列表: GET /proxies/{group}
  - 切换节点:     PUT /proxies/{group}  body: {"name": "node"}
  - 获取延迟:     GET /proxies/{group}/delay
"""
import json, time, random, urllib.request, urllib.error, sys, os
from dotenv import load_dotenv
load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLASH_HOST = os.getenv("CLASH_HOST") or "127.0.0.1"
CLASH_PORT = int(os.getenv("CLASH_PORT") or 9097)
CLASH_SECRET = os.getenv("CLASH_SECRET") or "set-your-secret"
PROXY_GROUP = os.getenv("CLASH_GROUP") or ""  # 订阅代理组名(必填才启用轮换,见 .env.example)
BASE_URL = f"http://{CLASH_HOST}:{CLASH_PORT}"

# 日常使用的稳定节点 —— 轮换时排除此节点，注册完成后切回
# 可通过环境变量 STABLE_NODE 设置，或由调用方传入
# 留空则由脚本自动检测当前节点作为 stable
STABLE_NODE = os.getenv("STABLE_NODE", "")


def _request(method, path, body=None):
    """发送 Clash API 请求"""
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {CLASH_SECRET}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                return None  # 204 No Content
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Clash API {method} {path} → {e.code}: {body_text[:200]}")
    except Exception as e:
        raise RuntimeError(f"Clash API {method} {path} 失败: {e}")


def get_proxies():
    """获取代理组的所有节点信息"""
    encoded_group = urllib.parse.quote(PROXY_GROUP)
    return _request("GET", f"/proxies/{encoded_group}")



# 注册友好的区域（低延迟、高成功率），按优先级排序
PREFERRED_REGIONS = {"JP", "SG", "HK", "TW", "KR"}
# 排除的关键词（限速/故障/非代理节点）
BAD_KEYWORDS = ["限速", "故障", "DIRECT", "自动选择", "故障转移", "REJECT"]
# 显式排除的节点(精确名,逗号分隔)——实测不可用/慢节点的黑名单,见 .env.example GROK_SKIP_NODES
SKIP_NODES = {n.strip() for n in os.getenv("GROK_SKIP_NODES", "").split(",") if n.strip()}


def list_fast_nodes():
    """
    筛选优质节点：优先区域 + 排除限速/故障节点。
    返回 (fast_nodes, skipped_nodes)
    """
    all_nodes, _ = list_nodes()
    fast = []
    skipped = []
    for n in all_nodes:
        # 排除非代理节点
        bad = False
        for kw in BAD_KEYWORDS:
            if kw.lower() in n.lower():
                skipped.append((n, f"关键词: {kw}"))
                bad = True
                break
        if bad:
            continue
        region = get_node_country(n)
        if region in PREFERRED_REGIONS:
            fast.append((n, region))
        else:
            skipped.append((n, f"非优先区域({region})"))
    return [n for n, _ in fast], skipped


def list_nodes():
    """列出所有可用节点名 (all, now)"""
    data = get_proxies()
    return data.get("all", []), data.get("now", "")


def switch_node(node_name):
    """切换到指定节点"""
    encoded_group = urllib.parse.quote(PROXY_GROUP)
    _request("PUT", f"/proxies/{encoded_group}", {"name": node_name})
    time.sleep(2)  # 等待代理生效


def get_current_ip(timeout=10):
    """通过外部服务获取当前出口 IP(走 GROK_PROXY 代理)"""
    proxy = os.environ.get("GROK_PROXY")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    ) if proxy else None
    services = [
        ("https://api.ipify.org?format=json", lambda r: r.get("ip")),
        ("https://httpbin.org/ip", lambda r: r.get("origin", "").split(",")[0].strip()),
        ("https://ifconfig.me/ip", lambda r: r.strip()),
    ]
    for url, parser in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            open_fn = opener.open if opener else urllib.request.urlopen
            with open_fn(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if url.endswith("/ip"):
                    return raw.strip()
                result = parser(json.loads(raw) if raw.startswith("{") else raw)
                if result:
                    return result
        except Exception:
            continue
    return None


def get_node_country(node_name):
    """从节点名推断国家/地区代码"""
    country_map = {
        "台湾": "TW", "日本": "JP", "新加坡": "SG", "韩国": "KR",
        "香港": "HK", "美国": "US", "瑞士": "CH", "德国": "DE",
        "英国": "UK", "荷兰": "NL", "澳大利亚": "AU", "加拿大": "CA",
        "俄罗斯": "RU", "印尼": "ID", "菲律宾": "PH", "文莱": "BN",
        "新西兰": "NZ", "东帝汶": "TL", "澳门": "MO", "印度": "IN",
        "TW": "TW", "JP": "JP", "SG": "SG", "KR": "KR", "HK": "HK",
        "US": "US", "CH": "CH", "DE": "DE", "UK": "UK", "NL": "NL",
        "AU": "AU", "CA": "CA", "RU": "RU", "ID": "ID", "PH": "PH",
    }
    for kw, code in country_map.items():
        if kw.lower() in node_name.lower():
            return code
    return "??"


def _resolve_stable(stable_node=None):
    """解析稳定节点名。优先级：参数 > STABLE_NODE 环境变量 > 自动检测当前节点"""
    node = stable_node or STABLE_NODE
    if node:
        # 验证是否存在
        all_nodes, _ = list_nodes()
        if node in all_nodes:
            return node
        # 节点不存在（订阅更新后被删），尝试找同区域节点
        region = get_node_country(node)
        for n in all_nodes:
            if get_node_country(n) == region:
                print(f"[Clash] ⚠️ 原稳定节点 '{node[:30]}' 不存在，已 fallback 到同区域: {n[:40]}")
                return n
        print(f"[Clash] ⚠️ 原稳定节点 '{node[:30]}' 不存在且无同区域节点，auto-detect")
    # 自动检测
    _, current = list_nodes()
    return current


def _build_candidates(exclude=None):
    """构建候选节点列表：排除稳定节点 + 劣质节点，优先优质区域"""
    all_nodes, current = list_nodes()
    if not all_nodes:
        raise RuntimeError("无可用节点")

    exclude_set = set()
    if exclude:
        if isinstance(exclude, str):
            exclude_set.add(exclude)
        else:
            exclude_set.update(exclude)
    # 显式黑名单节点(GROK_SKIP_NODES,2026-08-16 实测不可用)
    exclude_set.update(SKIP_NODES)
    # 始终排除稳定节点
    stable = _resolve_stable()
    if stable:
        exclude_set.add(stable)

    # 优先用优质节点
    fast, _ = list_fast_nodes()
    candidates = [n for n in fast if n not in exclude_set]

    if not candidates:
        # 回退：全部节点（排除稳定节点 + 限速/故障）
        for kw in BAD_KEYWORDS:
            exclude_set.update(n for n in all_nodes if kw.lower() in n.lower())
        candidates = [n for n in all_nodes if n not in exclude_set]
        if not candidates:
            candidates = list(all_nodes)

    return candidates, current


# ═══════════════════════ 公开 API ═══════════════════════

def snapshot():
    """快照当前节点，返回节点名。用于 restore() 恢复。"""
    _, current = list_nodes()
    return current


def restore(node):
    """恢复到之前快照的节点"""
    if not node:
        return
    all_nodes, _ = list_nodes()
    if node in all_nodes:
        print(f"[Clash] 恢复节点: → {node[:50]}")
        switch_node(node)
    else:
        # 原节点已消失（订阅更新），fallback 到同区域
        region = get_node_country(node)
        print(f"[Clash] ⚠️ 原节点 '{node[:30]}' 不存在，寻找 {region} 区域替代...")
        for n in all_nodes:
            if get_node_country(n) == region:
                print(f"[Clash] 恢复 fallback: → {n[:50]}")
                switch_node(n)
                return
        print(f"[Clash] ⚠️ 无法恢复，保持当前节点")


def switch_to_stable(stable_node=None):
    """切换到稳定节点（日常使用）"""
    target = _resolve_stable(stable_node)
    _, current = list_nodes()
    if target == current:
        return target
    print(f"[Clash] 切换到稳定节点: {current[:40]} → {target[:40]}")
    switch_node(target)
    return target


def _node_history():
    """近期使用过的节点(跨进程持久化,防轮换反复用同一批节点)。

    x.ai 对单 IP 发码限流 ~3-5 次/小时;随机轮换可能连续选中近期用过的节点。
    历史存在数据目录,最多记 12 个。
    """
    import json as _json
    hist_file = os.path.join(os.getenv("GROK_DATA_DIR", ""), ".node_history.json") \
        if os.getenv("GROK_DATA_DIR") else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".node_history.json")
    try:
        if os.path.exists(hist_file):
            with open(hist_file, encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return []


def _record_node(name):
    """记录一次节点使用(去重保序,上限 12)。"""
    import json as _json
    hist = _node_history()
    if name in hist:
        hist.remove(name)
    hist.append(name)
    hist = hist[-12:]
    hist_file = os.path.join(os.getenv("GROK_DATA_DIR", ""), ".node_history.json") \
        if os.getenv("GROK_DATA_DIR") else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".node_history.json")
    try:
        with open(hist_file, "w", encoding="utf-8") as f:
            _json.dump(hist, f, ensure_ascii=False)
    except Exception:
        pass


def random_switch(exclude_stable=True):
    """随机切换到一个非稳定节点,优先避开近期用过的节点(LRU)。返回新节点名"""
    candidates, current = _build_candidates(exclude=current if not exclude_stable else None)
    if exclude_stable:
        # 再次确保 stable 被排除
        stable = _resolve_stable()
        candidates = [n for n in candidates if n != stable]

    # 避开近期用过的节点(有足够候选时)
    recent = set(_node_history())
    fresh = [n for n in candidates if n not in recent and n != current]
    pool = fresh if fresh else [n for n in candidates if n != current]
    if not pool:
        pool = candidates
    target = random.choice(pool)
    _record_node(target)
    print(f"[Clash] 随机切换: {current[:30]}... → {target[:30]}..."
          f"({'LRU' if fresh else 'reuse'})")
    switch_node(target)
    return target


def switch_region(exclude_regions=None):
    """切换到与当前不同的区域节点（排除稳定节点）"""
    if exclude_regions is None:
        exclude_regions = set()

    all_nodes, current = list_nodes()
    stable = _resolve_stable()
    current_region = get_node_country(current)
    exclude_regions.add(current_region)

    # 按区域分组，排除稳定节点所在区域
    by_region = {}
    for n in all_nodes:
        if n == stable:
            continue
        r = get_node_country(n)
        by_region.setdefault(r, []).append(n)

    # 优先选不同区域
    candidates = []
    for r, nodes in by_region.items():
        if r not in exclude_regions:
            candidates.extend(nodes)

    if not candidates:
        candidates = [n for n in all_nodes if n != current and n != stable]
    if not candidates:
        candidates = [n for n in all_nodes if n != current]

    target = random.choice(candidates)
    target_region = get_node_country(target)
    print(f"[Clash] 区域切换: {current_region}({current[:20]}...) → {target_region}({target[:20]}...)")
    switch_node(target)
    return target, target_region


def health():
    """检查 Clash 连接状态"""
    try:
        _, current = list_nodes()
        ip = get_current_ip()
        return {
            "ok": True,
            "current_node": current,
            "current_ip": ip,
            "region": get_node_country(current),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════ CLI ═══════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Clash 节点轮换器")
    p.add_argument("--stable", default=STABLE_NODE, help="日常稳定节点名（轮换时排除）")
    p.add_argument("--snapshot", action="store_true", help="仅输出当前节点名")
    p.add_argument("--restore", metavar="NODE", help="恢复到指定节点")
    p.add_argument("--random", action="store_true", help="随机切换")
    p.add_argument("--region", action="store_true", help="跨区域切换")
    p.add_argument("--to-stable", action="store_true", help="切换到稳定节点")
    p.add_argument("--ip", action="store_true", help="仅输出当前 IP")
    args = p.parse_args()

    if args.snapshot:
        print(snapshot())
    elif args.restore:
        restore(args.restore)
    elif args.random:
        new = random_switch()
        ip = get_current_ip()
        print(f"{new} | {ip}")
    elif args.region:
        new, region = switch_region()
        ip = get_current_ip()
        print(f"{new} | {region} | {ip}")
    elif args.to_stable:
        switch_to_stable(args.stable)
    elif args.ip:
        print(get_current_ip() or "N/A")
    else:
        h = health()
        print(f"节点: {h.get('current_node','?')}")
        print(f"  IP: {h.get('current_ip','?')}")
        print(f"区域: {h.get('region','?')}")
