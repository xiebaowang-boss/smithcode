"""内置 MCP 服务器模板：向导第一步的快捷项。

模板只描述"怎么启动 + 需要哪些密钥"，不携带任何凭据；`{workspace}` 占位符
在生成命令时替换为当前工作区路径（filesystem 类服务器需要目录参数）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str
    command: tuple
    env: tuple = ()
    note: str = ""


TEMPLATES = (
    Template(
        key="filesystem",
        title="文件系统",
        description="按目录授权访问文件（官方参考实现）",
        command=("npx", "-y", "@modelcontextprotocol/server-filesystem", "{workspace}"),
    ),
    Template(
        key="github",
        title="GitHub",
        description="仓库、issue 与 PR 操作",
        command=("npx", "-y", "@modelcontextprotocol/server-github"),
        env=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        note="需要 GitHub Personal Access Token",
    ),
    Template(
        key="playwright",
        title="Playwright 浏览器",
        description="浏览器自动化与页面抓取",
        command=("npx", "-y", "@playwright/mcp@latest"),
        note="首次运行可能需要下载浏览器组件",
    ),
    Template(
        key="memory",
        title="记忆存储",
        description="基于知识图谱的持久记忆",
        command=("npx", "-y", "@modelcontextprotocol/server-memory"),
    ),
    Template(
        key="everything",
        title="Everything（测试）",
        description="官方测试服务器，含多种工具与资源",
        command=("npx", "-y", "@modelcontextprotocol/server-everything"),
    ),
)


def by_key(key: str):
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None


def command_for(template: Template, workspace: str) -> list:
    """展开模板命令：`{workspace}` 替换为工作区路径。"""
    return [part.replace("{workspace}", workspace) for part in template.command]


def title_for(key: str) -> str:
    template = by_key(key)
    return template.title if template is not None else key
