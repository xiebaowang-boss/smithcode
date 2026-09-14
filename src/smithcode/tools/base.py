"""工具注册表：新增工具只需在实现文件里用 @register 装饰器声明，
无需再修改汇总处。

动态工具（MCP 等运行时接入）：`register_dynamic` / `unregister_dynamic`
写入同一批全局结构，复用权限 / 并行调度 / 终端展示的全套机制；所有
增删与快照都在锁内进行，后台连接线程与 Agent 读取线程安全共存。
"""

import threading

SCHEMAS: list = []
FUNCTIONS: dict = {}
PATTERN_ARGS: dict = {}
PATTERN_FAMILIES: dict = {}
PATHS_EXTRACTORS: dict = {}
DESCRIBERS: dict = {}
PREVIEWS: dict = {}  # (args)->str|None：ask 确认前生成的变更预览（如 unified diff）
DISPLAY: dict = {}  # 终端展示形态：inline（一行式）/ block（可折叠结果块）
SERIAL: dict = {}  # 是否禁止并行：True 的工具批量执行时在主线程串行运行
HIDDEN: set = set()  # 不发送给 LLM 的工具（保留注册，如无可用技能时的 use_skill）

# 动态注册的工具名（MCP）：unregister 时据此清理，不影响静态工具
DYNAMIC: set = set()

_LOCK = threading.RLock()

# 只读文件类工具：目标落在技能目录只读白名单内时免越界确认（agent._preflight_path）
READ_ONLY_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep"})


def register(schema: dict):
    """把一个函数注册为 Agent 可调用的工具。

    schema 为该工具的 function-calling 描述，schema["name"] 必须与函数对应。
    可选的 schema["pattern_arg"] 声明权限匹配使用的参数名（如 "path"、"command"），
    该键在注册时被移除，不会出现在发送给 LLM 的 schema 中。
    可选的 schema["family"] 声明权限族：继承该工具名下的全部权限规则（默认族=自身）。
    可选的 schema["paths_from"] 是一个 (args)->[路径...] 提取函数，供多路径工具
    （如 apply_patch）做逐路径预检与权限聚合，同样不会发送给 LLM。
    可选的 schema["describe"] 是一个 (args)->str 函数，生成终端展示的一行短摘要
    （如 `read src/agent.py`），同样不会发送给 LLM。
    可选的 schema["preview"] 是一个 (args)->str|None 函数，在该工具触发权限
    ask 确认时生成变更预览（如 unified diff，None 表示无可预览内容），让用户
    看清改动再决策；同样不会发送给 LLM。
    可选的 schema["display"] 声明终端展示形态（opencode 式）：
    "inline"（默认，一行摘要 + 计数）/ "block"（结果可折叠成块，按行截断），
    同样不会发送给 LLM。
    可选的 schema["serial"] 声明该工具禁止并行（默认 False）：批量执行时
    可并行工具进线程池，serial=True 的工具在主线程串行运行、作为顺序屏障
    （单会话 shell、交互确认、写文件等有跨调用状态或线程不安全的工具必须声明）。
    """

    def decorator(func):
        s = dict(schema)
        PATTERN_ARGS[s["name"]] = s.pop("pattern_arg", None)
        PATTERN_FAMILIES[s["name"]] = s.pop("family", s["name"])
        PATHS_EXTRACTORS[s["name"]] = s.pop("paths_from", None)
        DESCRIBERS[s["name"]] = s.pop("describe", None)
        PREVIEWS[s["name"]] = s.pop("preview", None)
        DISPLAY[s["name"]] = s.pop("display", "inline")
        SERIAL[s["name"]] = bool(s.pop("serial", False))
        SCHEMAS.append(s)
        FUNCTIONS[s["name"]] = func
        return func

    return decorator


def set_hidden(name: str, hidden: bool = True) -> None:
    """隐藏/恢复一个已注册工具（不发送给 LLM，注册与执行能力保留）。"""
    with _LOCK:
        if hidden:
            HIDDEN.add(name)
        else:
            HIDDEN.discard(name)


def register_dynamic(schema: dict, func, *, serial: bool = True,
                     describe=None, display: str = "inline") -> None:
    """运行时注册一个工具（同名先移除），复用静态工具的全套机制。

    MCP 工具经由本入口进入注册表：schema 形如静态工具，`func(**args) -> str`。
    默认 serial=True——外部服务器的状态未知，批量执行时作为顺序屏障更安全。
    """
    name = schema["name"]
    with _LOCK:
        _remove_dynamic_locked(name)
        PATTERN_ARGS[name] = None
        PATTERN_FAMILIES[name] = name
        PATHS_EXTRACTORS[name] = None
        DESCRIBERS[name] = describe
        PREVIEWS[name] = None
        DISPLAY[name] = display
        SERIAL[name] = bool(serial)
        SCHEMAS.append(schema)
        FUNCTIONS[name] = func
        DYNAMIC.add(name)


def unregister_dynamic(name: str) -> None:
    """移除一个动态注册的工具；静态工具或不存在时静默返回。"""
    with _LOCK:
        _remove_dynamic_locked(name)


def _remove_dynamic_locked(name: str) -> None:
    if name not in DYNAMIC:
        return
    for index, schema in enumerate(SCHEMAS):
        if schema.get("name") == name:
            del SCHEMAS[index]
            break
    FUNCTIONS.pop(name, None)
    PATTERN_ARGS.pop(name, None)
    PATTERN_FAMILIES.pop(name, None)
    PATHS_EXTRACTORS.pop(name, None)
    DESCRIBERS.pop(name, None)
    PREVIEWS.pop(name, None)
    DISPLAY.pop(name, None)
    SERIAL.pop(name, None)
    HIDDEN.discard(name)
    DYNAMIC.discard(name)


def all_schemas() -> list:
    """全部已注册 schema 的快照（含隐藏项），供展示/同步逻辑安全遍历。"""
    with _LOCK:
        return list(SCHEMAS)


def visible_schemas() -> list:
    """发给 LLM 的工具 schema（过滤掉被隐藏的工具）。"""
    with _LOCK:
        return [s for s in list(SCHEMAS) if s["name"] not in HIDDEN]
