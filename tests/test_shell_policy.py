"""命令策略测试：安全只读矩阵、重定向与替换边界、跨平台安全集、前缀推导。"""
import pytest

from smithcode import config, sandbox
from smithcode.permission import shell_policy
from smithcode.permission.shell_policy import (
    command_key,
    derive_prefix,
    is_safe_command,
)


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """把工作区指向临时目录，并清空附加/会话/临时授权目录，保证 cd 判定确定。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "EXTRA_ROOTS", [])
    monkeypatch.setattr(sandbox, "_default", sandbox.Roots())
    return tmp_path


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(shell_policy, "_is_windows", lambda: False)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(shell_policy, "_is_windows", lambda: True)


# ---------- POSIX：放行 ----------

@pytest.mark.parametrize("command", [
    "ls -la",
    "ls -la src",
    "cat README.md",
    "head -n 5 a.py",
    "tail -f log.txt",
    "wc -l src/*.py",            # wc 不在 GLOB_RISK，未加引号 glob 放行
    "pwd",
    "echo hello world",
    "grep -rn foo src",
    "git status",
    "git status --short",
    "git log --oneline -5",
    "git diff HEAD",
    "git show HEAD",
    "git branch -a",
    "git stash list",
    "git remote -v",
    "git config --get user.name",
    "find . -name '*.py'",
    "sort a.txt",
    "date",
])
def test_posix_safe_pass(posix, command):
    assert is_safe_command(command) is True


# ---------- POSIX：拒绝 ----------

@pytest.mark.parametrize("command", [
    "ls > out.txt",
    "ls >> out.txt",
    "cat < secret",
    "cat < /etc/passwd",
    "CI=true git commit -m x",
    "PATH=/evil ls",
    "./sed -n 1p f",
    "/usr/bin/ls",
    "..\\bin\\ls",
    "sudo ls",
    "bash -c 'ls'",
    "sh -c ls",
    "env",
    "printenv",
    "awk 'BEGIN{system(\"rm -rf /\")}'",
    "sed -i s/x/y/ f",
    "sed 'w out.txt' f",
    "uniq a.txt b.txt",
    "curl http://example.com",
    "wget http://example.com",
    "docker ps",
    "rm -rf /",
    "mv a b",
    "cp a b",
    "mkdir x",
    "touch x",
    "chmod 777 x",
    "python -c 'print(1)'",
    "find . -delete",
    "find . -exec rm {} ;",
    "sort -o out a",
    "date -s '2020-01-01'",
    "file -m magic f",
    "git push",
    "git commit -m x",
    "git -c core.pager=cat log",
    "git branch -D main",
    "git tag v1",
    "git config user.name x",
    "ls $(rm -rf /)",
    "ls `rm -rf /`",
    "echo " + "a" * 10001,
])
def test_posix_unsafe_rejected(posix, command):
    assert is_safe_command(command) is False


# ---------- 重定向 / 命令替换 / glob 边界 ----------

@pytest.mark.parametrize("command", [
    "ls 2>/dev/null",
    "grep x f 2>&1",
    "cat < /dev/null",
    "echo hi > /dev/null",
])
def test_discard_redirects_allowed(posix, command):
    assert is_safe_command(command) is True


def test_quoted_glob_is_safe(posix):
    assert is_safe_command("find . -name '*.py'") is True


def test_unquoted_glob_on_risky_command_rejected(posix):
    assert is_safe_command("find . -name *.py") is False


def test_unbalanced_quote_rejected(posix):
    assert is_safe_command("echo 'unterminated") is False


@pytest.mark.parametrize("command", ["", "   ", "''"])
def test_empty_or_blank_rejected(posix, command):
    # 空命令无意义；`''` 是空 token 命令名，不在安全集
    assert is_safe_command(command) is False


# ---------- cd 特判 ----------

def test_cd_inside_workspace_safe(posix):
    assert is_safe_command("cd sub") is True
    assert is_safe_command("cd .") is True


@pytest.mark.parametrize("command", ["cd ../../etc", "cd /etc", "cd", "cd -", "cd a b"])
def test_cd_outside_or_ambiguous_rejected(posix, command):
    assert is_safe_command(command) is False


# ---------- Windows 安全集 ----------

@pytest.mark.parametrize("command", ["dir", "dir /w", "type a.txt", "where python",
                                     "findstr foo file.txt", "echo hi"])
def test_windows_safe_pass(windows, command):
    assert is_safe_command(command) is True


def test_windows_ls_rejected(windows):
    assert is_safe_command("ls") is False


# ---------- 开发工具链：只读用法放行 ----------

@pytest.mark.parametrize("command", [
    "python --version",
    "python3 -V",
    "node --version",
    "pytest --version",
    "cargo --version",
    "rustc --version",
    "go version",
    "pip --version",
    "pip list",
    "pip show requests",
    "pip freeze",
    "pip config list",
    "pip3 list",
    "npm --version",
    "npm ls",
    "npm config get registry",
    "uv --version",
    "uv pip list",
    "uv tool list",
    "poetry show",
    "poetry env info",
    "ruff check",
    "ruff check src tests",
])
def test_dev_tooling_safe_pass(posix, command):
    assert is_safe_command(command) is True


# ---------- 开发工具链：运行代码 / 网络 / 写入用法拒绝 ----------

@pytest.mark.parametrize("command", [
    "pytest",
    "pytest tests/",
    "python -c 'print(1)'",
    "python script.py",
    "python -m pytest",
    "node -e 'console.log(1)'",
    "node app.js",
    "npm run build",
    "npm test",
    "npm install",
    "pip install requests",
    "pip list --outdated",
    "pip install -i https://x.example/simple requests",
    "uv run pytest",
    "uv sync",
    "uvx ruff check",
    "poetry run pytest",
    "poetry install",
    "cargo test",
    "cargo run",
    "go test ./...",
    "go build",
    "ruff check --fix",
    "ruff format",
])
def test_dev_tooling_run_rejected(posix, command):
    assert is_safe_command(command) is False


# ---------- command_key：归一化身份 ----------

@pytest.mark.parametrize("segment, expected", [
    ("python -m pytest tests/a.py", ("python", "-m", "pytest", "tests/a.py")),
    ("python -m pytest -k foo", ("python", "-m", "pytest")),
    ("python script.py --debug", ("python", "script.py")),
    ("python -c 'print(1)'", None),
    ("python", None),
    ("CI=1 python -m pytest", ("python", "-m", "pytest")),
    ("env FOO=bar pytest -k x", ("pytest",)),
    ("git status", ("git", "status")),
    ("git --no-pager log", ("git",)),
    ("npm run test", ("npm", "run", "test")),
    ("pytest -k foo", ("pytest",)),
    ("node app.js", ("node", "app.js")),
    ("node -e 'x'", None),
    ("bash -c 'pytest'", None),
    ("rm -rf /tmp/x", None),
])
def test_command_key(posix, segment, expected):
    assert command_key(segment) == expected


# ---------- derive_prefix：切点推导 ----------

@pytest.mark.parametrize("segment, expected", [
    # 解释器：-m 切 3、脚本切 2、-c 拒绝
    ("python -m pytest tests/a.py", ("python", "-m", "pytest")),
    ("python -m pytest -k foo", ("python", "-m", "pytest")),
    ("python -m http.server", ("python", "-m", "http.server")),
    ("python script.py", ("python", "script.py")),
    ("python -c 'print(1)'", None),
    # env 前缀归一后仍命中
    ("CI=1 python -m pytest", ("python", "-m", "pytest")),
    # 通用 arity
    ("pytest -k foo", ("pytest",)),
    ("pytest tests/a.py", ("pytest",)),
    ("git commit -m x", ("git", "commit")),
    ("git push origin main", ("git", "push")),
    ("npm run test --watch", ("npm", "run", "test")),
    ("npm install express", ("npm", "install")),
    ("cargo test --all", ("cargo", "test")),
    ("ruff check src", ("ruff", "check")),
    # 回退/禁选 → 精确记忆
    ("git --no-pager log", None),
    ("ruff check --fix", None),
    ("uv run pytest", None),
    ("go run main.go", None),
    ("bash -c 'pytest'", None),
    ("rm -rf /tmp/x", None),
    ("some-unknown-tool --x", None),
])
def test_derive_prefix(posix, segment, expected):
    assert derive_prefix(segment) == expected


# ---------- is_dir_change / is_git_command ----------

def test_is_dir_change(posix):
    assert shell_policy.is_dir_change("cd sub") is True
    assert shell_policy.is_dir_change("cd .") is False
    assert shell_policy.is_dir_change("git status") is False


def test_is_git_command(posix):
    assert shell_policy.is_git_command("git status") is True
    assert shell_policy.is_git_command("echo git") is False
    assert shell_policy.is_git_command("") is False
