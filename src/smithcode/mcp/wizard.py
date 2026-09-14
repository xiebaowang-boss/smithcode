"""MCP 添加向导的纯状态机：TUI 面板与 REPL 行式流程共用的唯一定义。

向导只累积草稿并生成 `WizardPlan`，全程不读写文件、不连接服务器：
- TUI：`McpWizardPanel` 渲染当前步骤，完成回调交给宿主调用 `apply_plan`；
- REPL：`run_line_mode` 用编号菜单 + 掩码输入实现同一流程；
- 非交互：命令层只接受带全参的 `/mcp add ...` 直通路径，不做提问。

步骤按草稿动态生成（模板/手动/远程分支、按需的密钥步骤），支持回退修改；
Esc / q 取消时草稿直接丢弃，不产生任何副作用。
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .. import config
from . import templates as template_mod
from .config import ServerConfig

_SECRET_MODES = (
    ("存入凭据库（推荐，0600）", "store"),
    ("引用环境变量（适合 CI / 已有 shell 环境）", "env"),
    ("暂不提供（连接时会失败，稍后可补）", "skip"),
)
_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REF_IN_TEXT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class WizardStep:
    """一个向导步骤：宿主按 kind 选择渲染方式。"""

    key: str
    title: str
    kind: str  # choice / text / review
    options: list = field(default_factory=list)  # [(label, value)]，choice 用
    default: str = ""
    hint: str = ""
    masked: bool = False
    body: str = ""  # review 的预览正文


@dataclass
class WizardPlan:
    """向导完成后的写入计划：配置 + 作用域 + 需要落盘/引用的密钥。"""

    config: ServerConfig
    scope: str
    secrets: list = field(default_factory=list)   # [(变量名, 值)] → 凭据库
    env_refs: list = field(default_factory=list)  # 引用环境变量的变量名（展示用）


class McpWizard:
    """逐步收集配置的纯状态机；宿主每次读取 current() 并 submit()。"""

    def __init__(self, workspace: str = "", draft: dict | None = None):
        self.workspace = workspace or config.WORKSPACE_ROOT
        self.draft = {
            "source": "",
            "template": "",
            "command": "",
            "env_vars": [],
            "url": "",
            "transport": "http",
            "oauth": "yes",
            "headers": "",
            "name": "",
            "scope": "user",
            "secrets": {},  # var -> {"mode": "store|env|skip", "value": str}
        }
        if draft:
            self.draft.update(draft)
        self._index = 0
        self._done = False
        self.error = ""

    # ---------- 步骤 ----------

    def steps(self) -> list:
        steps = [WizardStep(
            key="source", title="添加方式", kind="choice",
            options=[
                ("从模板选择（推荐）", "template"),
                ("手动输入启动命令", "manual"),
                ("远程服务器（HTTP / SSE URL）", "remote"),
            ],
            default=self.draft["source"] or "template",
        )]

        if self.draft["source"] == "template":
            steps.append(WizardStep(
                key="template", title="选择服务器模板", kind="choice",
                options=[(f"{t.title} · {t.description}", t.key)
                         for t in template_mod.TEMPLATES],
                default=self.draft["template"],
            ))
        if self.draft["source"] == "manual":
            steps.append(WizardStep(
                key="command", title="服务器启动命令", kind="text",
                default=self.draft["command"],
                hint="例如: npx -y @modelcontextprotocol/server-everything",
            ))
            steps.append(WizardStep(
                key="env_vars", title="需要的环境变量名（可选）", kind="text",
                default=", ".join(self.draft["env_vars"]),
                hint="逗号分隔；值的存放方式在后续步骤选择",
            ))
        if self.draft["source"] == "remote":
            steps.append(WizardStep(
                key="url", title="服务器 URL", kind="text",
                default=self.draft["url"],
                hint="例如: https://mcp.linear.app/mcp",
            ))
            steps.append(WizardStep(
                key="transport", title="传输类型", kind="choice",
                options=[("Streamable HTTP（推荐）", "http"), ("SSE（旧版）", "sse")],
                default=self.draft["transport"],
            ))
            steps.append(WizardStep(
                key="oauth", title="鉴权方式", kind="choice",
                options=[
                    ("OAuth 授权（浏览器登录，推荐）", "yes"),
                    ("请求头（可引用 ${VAR} 或输入字面值）", "no"),
                ],
                default=self.draft["oauth"],
            ))
            steps.append(WizardStep(
                key="headers", title="请求头（可选）", kind="text",
                default=self.draft["headers"],
                hint="K=V，逗号分隔；字面值会存入凭据库，配置只留引用",
            ))

        steps.append(WizardStep(
            key="name", title="服务器名称", kind="text",
            default=self.draft["name"] or self.default_name(),
            hint="用于工具名前缀（mcp__<名称>__<工具>），建议短且唯一",
        ))
        steps.append(WizardStep(
            key="scope", title="配置作用域", kind="choice",
            options=[
                ("全局 · 仅自己、所有项目可用", "user"),
                ("项目 · .smithcode/mcp.json，随仓库共享", "project"),
            ],
            default=self.draft["scope"],
        ))

        for var in self.draft["env_vars"]:
            choice = self.draft["secrets"].get(var, {})
            steps.append(WizardStep(
                key=f"secret:{var}", title=f"密钥 {var}", kind="choice",
                options=list(_SECRET_MODES),
                default=choice.get("mode", "store"),
            ))
            if choice.get("mode") == "store":
                steps.append(WizardStep(
                    key=f"secret-value:{var}", title=f"输入 {var}", kind="text",
                    masked=True, default=choice.get("value", ""),
                    hint="输入内容不会回显，保存在 ~/.smithcode/credentials.json",
                ))

        steps.append(WizardStep(key="review", title="确认", kind="review",
                                body=self.preview()))
        return steps

    def current(self) -> WizardStep | None:
        steps = self.steps()
        if not steps:
            return None
        self._index = max(0, min(self._index, len(steps) - 1))
        return steps[self._index]

    @property
    def index(self) -> int:
        return self._index

    @property
    def done(self) -> bool:
        return self._done

    # ---------- 交互 ----------

    def submit(self, value: str) -> str | None:
        """提交当前步骤的值；返回错误文案（None 表示成功推进）。"""
        self.error = ""
        step = self.current()
        if step is None:
            return self._fail("没有待填写的步骤")
        value = "" if value is None else str(value)
        key = step.key

        if key == "source":
            if value not in ("template", "manual", "remote"):
                return self._fail("请选择添加方式")
            if self.draft["source"] != value:
                self.draft.update(
                    source=value, template="", command="", env_vars=[], secrets={},
                    url="", transport="http", oauth="yes", headers="",
                )
                self.draft["name"] = ""

        elif key == "template":
            template = template_mod.by_key(value)
            if template is None:
                return self._fail("未知模板")
            self.draft["template"] = value
            self.draft["secrets"] = {}
            self.draft["name"] = template.key
            if template.url:
                self.draft.update(
                    url=template.url, transport=template.transport,
                    oauth="yes" if template.oauth else "no",
                    command="", headers=_format_headers(template.headers),
                    env_vars=list(template.env),
                )
            else:
                self.draft["command"] = " ".join(
                    template_mod.command_for(template, self.workspace)
                )
                self.draft["env_vars"] = list(template.env)

        elif key == "url":
            url = value.strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._fail("URL 需要以 http:// 或 https:// 开头")
            self.draft["url"] = url

        elif key == "transport":
            if value not in ("http", "sse"):
                return self._fail("请选择传输类型")
            self.draft["transport"] = value

        elif key == "oauth":
            if value not in ("yes", "no"):
                return self._fail("请选择鉴权方式")
            self.draft["oauth"] = value

        elif key == "headers":
            error = _parse_headers(value)
            if isinstance(error, str):
                return self._fail(error)
            self.draft["headers"] = value.strip()

        elif key == "command":
            argv = _split_command(value)
            if not argv:
                return self._fail("命令不能为空")
            self.draft["command"] = " ".join(argv)

        elif key == "env_vars":
            names = [item for item in re.split(r"[,\s]+", value) if item]
            for name in names:
                if not _VAR_NAME.fullmatch(name):
                    return self._fail(f"环境变量名不合法: {name}")
            self.draft["env_vars"] = names
            self.draft["secrets"] = {
                k: v for k, v in self.draft["secrets"].items() if k in names
            }

        elif key == "name":
            name = value.strip()
            if not name:
                return self._fail("名称不能为空")
            self.draft["name"] = name

        elif key == "scope":
            if value not in ("user", "project"):
                return self._fail("请选择作用域")
            self.draft["scope"] = value

        elif key.startswith("secret:"):
            if value not in ("store", "env", "skip"):
                return self._fail("请选择密钥的存放方式")
            var = key.split(":", 1)[1]
            entry = self.draft["secrets"].setdefault(var, {})
            entry["mode"] = value
            if value != "store":
                entry["value"] = ""

        elif key.startswith("secret-value:"):
            var = key.split(":", 1)[1]
            if not value:
                return self._fail("密钥不能为空（可返回选择其他方式）")
            self.draft["secrets"].setdefault(var, {})["value"] = value

        elif key == "review":
            if value != "finish":
                return self._fail("请确认保存或返回修改")
            self._done = True
            return None

        else:
            return self._fail("未知步骤")

        self._index += 1
        return None

    def back(self) -> bool:
        if self._index <= 0:
            return False
        self._index -= 1
        return True

    # ---------- 产物 ----------

    def _is_remote(self) -> bool:
        return self.draft["source"] == "remote" or bool(self.draft["url"])

    def default_name(self) -> str:
        if self.draft["template"]:
            return self.draft["template"]
        if self.draft["url"]:
            return _name_from_url(self.draft["url"])
        argv = self._argv()
        if not argv:
            return ""
        candidates = [item for item in argv[1:] if not item.startswith("-")]
        for token in candidates:
            if token.startswith("@") and "/" in token:
                package = token.split("/")[-1].split("@")[0]
                for prefix in ("mcp-server-", "server-"):
                    if package.startswith(prefix):
                        package = package[len(prefix):]
                        break
                if package:
                    return package
        for token in candidates:
            if not token.replace(".", "").isdigit():
                base = os.path.basename(token).split(".")[0]
                if base:
                    return base
        return os.path.basename(argv[0]).split(".")[0] or "mcp"

    def _argv(self) -> list:
        if self.draft["source"] == "template":
            template = template_mod.by_key(self.draft["template"])
            if template is not None and not template.url:
                return template_mod.command_for(template, self.workspace)
        if self._is_remote():
            return []
        return _split_command(self.draft["command"])

    def _remote_headers(self) -> list:
        """返回 [(header, value_or_ref)] 与需存入凭据库的字面值。"""
        headers = []
        secrets = []
        parsed = _parse_headers(self.draft["headers"])
        if isinstance(parsed, str):  # 理论上前置校验已拦截；防御式处理
            return headers, secrets
        for key, value in parsed:
            if _REF_IN_TEXT.search(value):
                headers.append((key, value))
            else:
                var = _header_var(key)
                headers.append((key, "${" + var + "}"))
                secrets.append((var, value))
        return headers, secrets

    def to_plan(self) -> WizardPlan:
        name = self.draft["name"].strip() or self.default_name()
        if self._is_remote():
            headers, secrets = self._remote_headers()
            # 模板 / 引用型请求头不在向导里输入值，需为被引用的变量安排密钥步骤
            env_refs = []
            for var in self.draft["env_vars"]:
                entry = self.draft["secrets"].get(var, {})
                if entry.get("mode") == "store" and entry.get("value"):
                    secrets.append((var, entry["value"]))
                elif entry.get("mode") == "env":
                    env_refs.append(var)
            cfg = ServerConfig(
                name=name, type=self.draft["transport"], url=self.draft["url"],
                headers=dict(headers), oauth=self.draft["oauth"] == "yes",
                scope=self.draft["scope"],
            )
            return WizardPlan(
                config=cfg, scope=self.draft["scope"],
                secrets=secrets, env_refs=env_refs,
            )

        argv = self._argv()
        env = {var: "${" + var + "}" for var in self.draft["env_vars"]}
        cfg = ServerConfig(
            name=name, command=argv, env=env, scope=self.draft["scope"],
        )
        secrets = []
        env_refs = []
        for var in self.draft["env_vars"]:
            entry = self.draft["secrets"].get(var, {})
            if entry.get("mode") == "store" and entry.get("value"):
                secrets.append((var, entry["value"]))
            elif entry.get("mode") == "env":
                env_refs.append(var)
        return WizardPlan(
            config=cfg, scope=self.draft["scope"],
            secrets=secrets, env_refs=env_refs,
        )

    def preview(self) -> str:
        """确认页正文：脱敏后的配置片段 + 写入位置 + 风险提示。"""
        name = self.draft["name"] or self.default_name()
        lines = []
        if self._is_remote():
            entry = {"type": self.draft["transport"], "url": self.draft["url"]}
            if self.draft["oauth"] == "yes":
                entry["oauth"] = True
            headers, _ = self._remote_headers()
            if headers:
                entry["headers"] = dict(headers)
            if self.draft["scope"] == "project":
                path = Path(self.workspace) / ".smithcode" / "mcp.json"
                lines.append(f"写入位置: {path}")
                lines.append(json.dumps(
                    {"mcpServers": {name: entry}}, ensure_ascii=False, indent=2
                ))
            else:
                lines.append(f"写入位置: {config.config_path()}")
                lines.append(f"[mcp.servers.{name}]")
                lines.append(f"type = {json.dumps(entry['type'], ensure_ascii=False)}")
                lines.append(f"url = {json.dumps(entry['url'], ensure_ascii=False)}")
                if self.draft["oauth"] == "yes":
                    lines.append("oauth = true")
                if headers:
                    lines.append(f"headers = {json.dumps(dict(headers), ensure_ascii=False)}")
            handled = self._secret_lines()
            if handled:
                lines.append("")
                lines.extend(handled)
            lines.append("")
            lines.append("提醒: 该服务器将以你的本机权限运行；远程内容按不可信处理。")
            if self.draft["oauth"] == "yes":
                lines.append("OAuth 授权：保存后运行 /mcp auth " + name + " 完成浏览器登录。")
            if self.draft["scope"] == "project":
                lines.append("项目配置会随仓库提交，密钥只保存为 ${VAR} 引用。")
            return "\n".join(lines)

        argv = self._argv()
        if self.draft["scope"] == "project":
            path = Path(self.workspace) / ".smithcode" / "mcp.json"
            lines.append(f"写入位置: {path}")
            entry = {"type": "stdio", "command": argv[0] if argv else ""}
            if len(argv) > 1:
                entry["args"] = argv[1:]
            if self.draft["env_vars"]:
                entry["env"] = {v: "${" + v + "}" for v in self.draft["env_vars"]}
            lines.append(json.dumps({"mcpServers": {name: entry}}, ensure_ascii=False, indent=2))
        else:
            lines.append(f"写入位置: {config.config_path()}")
            lines.append(f"[mcp.servers.{name}]")
            lines.append(f"command = {json.dumps(argv, ensure_ascii=False)}")
            if self.draft["env_vars"]:
                env = {v: "${" + v + "}" for v in self.draft["env_vars"]}
                lines.append(f"env = {json.dumps(env, ensure_ascii=False)}")

        handled = self._secret_lines()
        if handled:
            lines.append("")
            lines.extend(handled)

        lines.append("")
        lines.append("提醒: 该服务器将以你的本机权限运行；远程内容按不可信处理。")
        if self.draft["scope"] == "project":
            lines.append("项目配置会随仓库提交，密钥只保存为 ${VAR} 引用。")
        return "\n".join(lines)

    def _secret_lines(self) -> list:
        """确认页的密钥处置说明：环境变量（stdio / 模板引用）与请求头字面值。"""
        lines = []
        covered = set()
        if self._is_remote():
            _, header_secrets = self._remote_headers()
            for var, _value in header_secrets:
                covered.add(var)
                lines.append(f"  密钥 {var} → 保存到凭据库（内容不回显）")
        for var in self.draft["env_vars"]:
            if var in covered:
                continue
            entry = self.draft["secrets"].get(var, {})
            mode = entry.get("mode", "skip")
            if mode == "store" and entry.get("value"):
                lines.append(f"  密钥 {var} → 保存到凭据库（内容不回显）")
            elif mode == "env":
                lines.append(f"  密钥 {var} → 引用环境变量 ${{{var}}}")
            else:
                lines.append(f"  密钥 {var} → 暂不提供（连接时可能失败）")
        return lines

    def _fail(self, message: str) -> str:
        self.error = message
        return message


def apply_plan(service, plan: WizardPlan):
    """宿主统一落盘入口：先存密钥再写配置并后台连接。"""
    from .secrets import store_secret

    for var, value in plan.secrets:
        store_secret(plan.config.name, var, value)
    return service.add(plan.config, plan.scope)


def _format_headers(pairs) -> str:
    """把模板的请求头 [(名, 值)] 还原成向导的 `K=V, K2=V2` 文本。"""
    return ", ".join(f"{key}={value}" for key, value in pairs)


def _parse_headers(text: str):
    """解析向导里的请求头输入（`K=V, K2=V2`）；返回 pairs 或错误文案。"""
    text = (text or "").strip()
    if not text:
        return []
    pairs = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            return f"请求头需要 K=V 形式: {chunk!r}"
        key, value = chunk.split("=", 1)
        key = key.strip()
        if not key:
            return "请求头名称不能为空"
        pairs.append((key, value.strip()))
    return pairs


def _header_var(key: str) -> str:
    return "MCP_HEADER_" + re.sub(r"[^A-Za-z0-9_]", "_", key).upper()


def _name_from_url(url: str) -> str:
    """从 URL 推断默认服务器名：mcp.linear.app/mcp → linear。"""
    host = (urlparse(url).hostname or "").lower()
    host = host.removeprefix("mcp.")
    label = host.split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", label) or "mcp"


def _split_command(text: str) -> list:
    """按 shell 风格的引号规则拆命令；Windows 下保留路径反斜杠。"""
    if not text.strip():
        return []
    lexer = shlex.shlex(text.strip(), posix=(os.name != "nt"))
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv = []
    for token in lexer:
        token = token.strip('"').strip("'")
        if token:
            argv.append(token)
    return argv


# ---------- REPL 行式渲染器 ----------

def run_line_mode(payload: dict | None = None, input_fn=input, print_fn=print,
                  secret_fn=None) -> WizardPlan | None:
    """REPL 用的行式向导；返回计划或 None（用户取消）。IO 可注入便于测试。"""
    wizard = McpWizard()
    if secret_fn is None:
        secret_fn = getpass.getpass
    while not wizard.done:
        step = wizard.current()
        if step is None:
            return None
        print_fn("")
        print_fn(f"[{step.title}]")
        if step.hint:
            print_fn(f"  {step.hint}")

        if step.kind == "choice":
            for index, (label, value) in enumerate(step.options, 1):
                marker = "（默认）" if value == step.default else ""
                print_fn(f"  {index}. {label}{marker}")
            raw = input_fn("  选择（回车=默认 / b=上一步 / q=取消）: ").strip()
            if raw.lower() == "q":
                return None
            if raw.lower() == "b":
                wizard.back()
                continue
            if not raw:
                value = step.default or step.options[0][1]
            elif raw.isdigit() and 1 <= int(raw) <= len(step.options):
                value = step.options[int(raw) - 1][1]
            else:
                value = raw
            error = wizard.submit(value)

        elif step.kind == "review":
            print_fn(step.body)
            raw = input_fn("  回车=保存并连接 / b=上一步 / q=取消: ").strip()
            if raw.lower() == "q":
                return None
            if raw.lower() == "b":
                wizard.back()
                continue
            error = wizard.submit("finish")

        else:  # text
            prompt = "  输入（回车=默认 / :b=上一步 / q=取消）: "
            raw = secret_fn(prompt) if step.masked else input_fn(prompt)
            if raw.strip().lower() == "q":
                return None
            if raw.strip() == ":b":
                wizard.back()
                continue
            if not raw.strip():
                raw = step.default
            error = wizard.submit(raw)

        if error:
            print_fn(f"  ! {error}")

    return wizard.to_plan()
