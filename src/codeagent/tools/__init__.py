"""导入各工具模块即完成注册，此处统一导出给 Agent 使用。"""
from . import base, files, shell

SCHEMAS = base.SCHEMAS
FUNCTIONS = base.FUNCTIONS
PATTERN_ARGS = base.PATTERN_ARGS

SAFE_TOOLS = {"read_file", "list_dir"}
