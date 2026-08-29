"""工具注册表：新增工具只需在实现文件里用 @register 装饰器声明，
无需再修改汇总处。"""

SCHEMAS: list = []
FUNCTIONS: dict = {}


def register(schema: dict):
    """把一个函数注册为 Agent 可调用的工具。

    schema 为该工具的 function-calling 描述，schema["name"] 必须与函数对应。
    """

    def decorator(func):
        SCHEMAS.append(schema)
        FUNCTIONS[schema["name"]] = func
        return func

    return decorator
