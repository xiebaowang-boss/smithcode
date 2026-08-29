import sys
import os

# Windows: 设置控制台代码页为 UTF-8
if sys.platform == 'win32':
    os.system('chcp 65001 > nul 2>&1')
    # 确保环境变量设置正确
    os.environ['PYTHONIOENCODING'] = 'utf-8'

from codeagent.cli import main

if __name__ == "__main__":
    main()
