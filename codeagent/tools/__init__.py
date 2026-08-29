from . import files, shell

SCHEMAS = files.SCHEMAS + shell.SCHEMAS
FUNCTIONS = {**files.FUNCTIONS, **shell.FUNCTIONS}

SAFE_TOOLS = {"read_file", "list_dir"}
