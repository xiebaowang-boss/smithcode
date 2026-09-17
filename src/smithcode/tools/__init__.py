"""导入各工具模块即完成注册，此处统一导出给 Agent 使用。"""
# 导入即注册：各模块在导入时通过 @register 把工具写入注册表
from . import (  # noqa: F401
    ask,
    base,
    files,
    glob,
    goal,
    grep,
    patch,
    shell,
    skills,
    todo,
    webfetch,
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
DYNAMIC = base.DYNAMIC
READ_ONLY_TOOLS = base.READ_ONLY_TOOLS
set_hidden = base.set_hidden
register_dynamic = base.register_dynamic
unregister_dynamic = base.unregister_dynamic
all_schemas = base.all_schemas
visible_schemas = base.visible_schemas
