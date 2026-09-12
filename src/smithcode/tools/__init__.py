"""导入各工具模块即完成注册，此处统一导出给 Agent 使用。"""
# 导入即注册：files / search / shell / patch / ask / todo / goal / skills 模块在导入时通过 @register 把工具写入注册表
from . import (  # noqa: F401
    ask,
    base,
    files,
    goal,
    patch,
    search,
    shell,
    skills,
    todo,
    web,
    websearch,
)

SCHEMAS = base.SCHEMAS
reset_read_tracking = files.reset_read_tracking
FUNCTIONS = base.FUNCTIONS
PATTERN_ARGS = base.PATTERN_ARGS
PATTERN_FAMILIES = base.PATTERN_FAMILIES
PATHS_EXTRACTORS = base.PATHS_EXTRACTORS
DESCRIBERS = base.DESCRIBERS
PREVIEWS = base.PREVIEWS
DISPLAY = base.DISPLAY
SERIAL = base.SERIAL
HIDDEN = base.HIDDEN
READ_ONLY_TOOLS = base.READ_ONLY_TOOLS
set_hidden = base.set_hidden
visible_schemas = base.visible_schemas
