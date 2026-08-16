"""一次性重铸指定账号被撤销的 xai OAuth token。

仅用于手选少量账号的场景；整池重铸请用 reauth_batch.py（自动读取 grok2api
中 reauthRequired 的账号，可断点续跑）。
"""
import os, sys, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GROK_PROXY", "http://127.0.0.1:7890")

from device_mint import sso_to_device
from auth_store import save_auth
from config import KEYS_DIR

# 按需填写要重铸的邮箱（SSO 从 keys/accounts.txt 读取）
NEED = []

# 从 accounts.txt 读 SSO (带 BOM)
sso_map = {}
with open(os.path.join(KEYS_DIR, "accounts.txt"),
          encoding="utf-8-sig") as f:
    for line in f.read().splitlines():
        parts = line.split(":")
        if len(parts) >= 3:
            sso_map[parts[0]] = ":".join(parts[2:])

ok, fail = 0, 0
for email in NEED:
    sso = sso_map.get(email, "")
    if not sso:
        print(f"[SKIP] {email}: accounts.txt 无 SSO")
        fail += 1
        continue
    print(f"\n=== 重铸: {email} ===")
    result = sso_to_device(sso, email)
    if result:
        save_auth(email, result)
        print(f"[OK] {email} 已写入 auths/")
        ok += 1
    else:
        print(f"[FAIL] {email} 铸造失败")
        fail += 1
    time.sleep(5)

print(f"\n完成: OK={ok} FAIL={fail}")
