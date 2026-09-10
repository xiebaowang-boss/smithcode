"""导入各工具模块即完成注册，此处统一导出给 Agent 使用。"""
# 导入即注册：files / search / shell / patch / ask / todo 模块在导入时通过 @register 把工具写入注册表
from . import ask, base, files, patch, search, shell, todo, web  # noqa: F401

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
