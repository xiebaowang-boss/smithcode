"""工具注册表：新增工具只需在实现文件里用 @register 装饰器声明，
无需再修改汇总处。"""

SCHEMAS: list = []
FUNCTIONS: dict = {}
PATTERN_ARGS: dict = {}
PATTERN_FAMILIES: dict = {}
PATHS_EXTRACTORS: dict = {}
DESCRIBERS: dict = {}


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
    """

    def decorator(func):
        s = dict(schema)
        PATTERN_ARGS[s["name"]] = s.pop("pattern_arg", None)
        PATTERN_FAMILIES[s["name"]] = s.pop("family", s["name"])
        PATHS_EXTRACTORS[s["name"]] = s.pop("paths_from", None)
        DESCRIBERS[s["name"]] = s.pop("describe", None)
        SCHEMAS.append(s)
        FUNCTIONS[s["name"]] = func
        return func

    return decorator
