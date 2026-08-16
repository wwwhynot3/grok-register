"""x.ai OAuth 公共常量（集中定义，供各 mint 模块引用；token_daemon 已移除）。

CLIENT_ID 是 x.ai 官方 grok-cli 的公开 OAuth 客户端标识（非机密），
多次在旧脚本中重复定义，集中到此避免将来变更时遗漏。
"""

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
