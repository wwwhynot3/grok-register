#!/usr/bin/env python3
"""Telegram 查询机器人入口(逻辑在 telegram.py,此处保持入口稳定,deploy 服务不变)。"""
import sys

from telegram import run_query_bot

if __name__ == "__main__":
    sys.exit(run_query_bot())
