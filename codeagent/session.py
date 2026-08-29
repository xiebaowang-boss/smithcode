import json
import time
import platform
from pathlib import Path

from . import config

def get_system_info():
    """获取操作系统信息"""
    os_info = platform.platform()
    python_version = platform.python_version()
    return f"操作系统: {os_info}\nPython版本: {python_version}"

SYSTEM_PROMPT = f"""你是一个运行在终端里的代码助手Happy Code，可以调用工具来读写文件、执行命令。

当前系统环境:
{get_system_info()}

规则：
1. 工作区之外的路径一律拒绝访问。
2. 修改文件前先用 read_file 查看现有内容。
3. 任务完成后简要总结做了什么。
4. 执行shell命令时，请根据当前操作系统选择正确的命令语法。例如：
   - Windows: 使用dir、copy、del等命令
   - Linux/macOS: 使用ls、cp、rm等命令
5. 如果不确定操作系统，请使用跨平台的命令或先检查系统信息。
6. 当用户只是询问方案可行性，并未明确表示想这么做时，仅回复用户此方案详情，禁止直接执行操作，可以询问用户是否需要进行下一步。"""


class Session:
    def __init__(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.created_at = time.time()

    def add(self, role: str, content: str = "", **kwargs) -> dict:
        msg = {"role": role, "content": content, **kwargs}
        self.messages.append(msg)
        return msg

    def reset(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.created_at = time.time()

    def save(self) -> Path:
        sessions_dir = Path(config.WORKSPACE_ROOT) / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        path = sessions_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(self.messages, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path


def load(path: Path) -> Session:
    session = Session()
    session.messages = json.loads(path.read_text(encoding="utf-8"))
    return session