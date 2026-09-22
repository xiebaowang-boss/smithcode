"""MCP 子系统的异常类型。

调用方（service / commands / 向导）只捕获这里的异常并翻译成用户可读文案，
不让底层 OSError / JSONDecodeError 裸漏到终端。
"""
from __future__ import annotations


class McpError(Exception):
    """MCP 子系统的基础异常：连接失败、协议错误、超时等。"""


class McpConfigError(McpError):
    """MCP 配置读写失败（文件损坏、目标条目缺失等）。"""


class McpAuthError(McpError):
    """OAuth 授权相关失败：需要用户授权、授权流程失败或超时。"""
