"""工具注册表：新增工具只需在实现文件里用 @register 装饰器声明，
无需再修改汇总处。"""

SCHEMAS: list = []
FUNCTIONS: dict = {}
PATTERN_ARGS: dict = {}


def register(schema: dict):
    """把一个函数注册为 Agent 可调用的工具。

    schema 为该工具的 function-calling 描述，schema["name"] 必须与函数对应。
    可选的 schema["pattern_arg"] 声明权限匹配使用的参数名（如 "path"、"command"），
    该键在注册时被移除，不会出现在发送给 LLM 的 schema 中。
    """

    def decorator(func):
        s = dict(schema)
        PATTERN_ARGS[s["name"]] = s.pop("pattern_arg", None)
        SCHEMAS.append(s)
        FUNCTIONS[s["name"]] = func
        return func

    return decorator
