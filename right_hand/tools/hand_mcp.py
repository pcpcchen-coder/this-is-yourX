#!/usr/bin/env python3
"""MCP stdio server 的進入點（給 Claude 桌面 app／Claude Code 登記用）。

不要在這裡印任何東西到 stdout：stdout 是 MCP 協定的通道。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hand_api.mcp_server import main  # noqa: E402

main()
