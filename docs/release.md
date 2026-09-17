# 发布文档

本文件包含两部分：**发布流程（SOP）** 与 **各版本发布说明**。

- 版本历史与逐条变更见 [CHANGELOG.md](../CHANGELOG.md)（面向开发者，按 Keep a Changelog 组织）
- 本文件的发布说明面向使用者，只讲「这个版本带来什么 / 需要注意什么」

## 版本号规则

`主版本.次版本.修订号`（语义化版本）：

- **主版本**：不兼容的破坏性变更（如移除命令、改了配置项语义）
- **次版本**：向后兼容的新功能（新工具、新命令、新子系统）
- **修订号**：向后兼容的修复与行为微调

版本号需同步三处，缺一不可：

| 位置 | 说明 |
| ---- | ---- |
| `pyproject.toml` 的 `[project].version` | 打包元数据的唯一来源 |
| `src/smithcode/__init__.py` 的 `__version__` | 运行期 `-V` / 欢迎屏 / 侧边栏 / MCP 客户端标识读这里 |
| `uv.lock` | 由 `uv lock` 自动同步，不手改 |

## 发布流程

```bash
# 1. 归档 CHANGELOG：把 [未发布] 改成 [X.Y.Z] - YYYY-MM-DD
#    （下一轮开发有新改动时再新建 [未发布] 段，参考 AGENTS.md「改动后必须验证」）

# 2. 升版本号（两处）
#    pyproject.toml            version = "X.Y.Z"
#    src/smithcode/__init__.py __version__ = "X.Y.Z"

# 3. 同步锁文件
uv lock

# 4. 全量验证（交付前必跑，别把全量测试塞进内循环）
uv run pytest
uv run ruff check src tests

# 5. 打包（sdist + wheel 落到 dist/）
rm -rf dist
uv build

# 6. 校验产物可独立安装
uv venv /tmp/smith-verify
uv pip install --python /tmp/smith-verify/bin/python dist/smithcode-X.Y.Z-py3-none-any.whl
/tmp/smith-verify/bin/smith -V   # 应输出 smith X.Y.Z
rm -rf /tmp/smith-verify

# 7. 更新全局安装（本机为 uv tool 安装的可编辑版本）
uv tool install --reinstall -e .
cd /tmp && smith -V             # 应输出 smith X.Y.Z

# 8. 打标签并推送
git tag -a vX.Y.Z -m "SmithCode X.Y.Z"
git push origin main
git push origin vX.Y.Z

# 9. 建 GitHub Release（粘贴 docs/release-notes-X.Y.Z.md 正文，附 dist/ 产物）
gh release create vX.Y.Z dist/smithcode-X.Y.Z-py3-none-any.whl dist/smithcode-X.Y.Z.tar.gz \
  --title "SmithCode X.Y.Z" --notes-file docs/release-notes-X.Y.Z.md
```

### 注意事项

- **打包产物不入库**：`build/`、`dist/`、`*.egg-info/` 已在 `.gitignore` 中
- **本机安装方式**：`smith` 由 `uv tool install -e .` 安装（可编辑），升级用 `uv tool install --reinstall -e .`；若历史上用过 `pip install --user`（PEP 668 会拦住覆盖），需 `--break-system-packages` 才能升级 user site 里的那份，升级后 `~/.local/bin/smith` 会被 pip 的脚本覆盖，需重新执行一次 `uv tool install --reinstall -e .` 恢复软链
- **提交与推送**：按项目约定，未经明确要求不执行 `git commit` / `git push`；打标签同理
- **可复现性**：`uv.lock` 锁定依赖，发布前确保已 `uv lock` 且工作区干净（除本次发布改动外）
- **产物校验值**：正式分发（上传到第三方平台 / 发公告）时在发布说明中给出 `sha256sum dist/*`

## GitHub Release

每个版本对应一个 GitHub Release，标签 `vX.Y.Z` 指向归档提交。Release 正文由 `docs/release-notes-X.Y.Z.md` 提供（面向使用者，与 `docs/release.md` 的「发布说明」同源），并把 `dist/` 下的 wheel 与 sdist 作为附件上传。

### 正文来源

- 每版新增 `docs/release-notes-X.Y.Z.md`：**不带一级标题**（GitHub 已用 Release 标题展示版本号），首行直接是概述，随后按主题分节
- 正文只写「带来什么 / 需要注意什么」，不写内部实现细节（实现细节见 `CHANGELOG.md`）
- 文末固定「升级方式」与「发布产物」两节：前者给安装 / 升级命令，后者给附件文件名与 SHA-256，便于核对下载完整性
- 附件务必包含 `dist/smithcode-X.Y.Z-py3-none-any.whl` 与 `dist/smithcode-X.Y.Z.tar.gz`，不额外上传校验文件（摘要写在正文里）

### 创建方式（gh CLI）

```bash
# 前置：标签已推送到 origin，dist/ 已由 uv build 产出，release notes 已就绪
gh release create vX.Y.Z \
  dist/smithcode-X.Y.Z-py3-none-any.whl \
  dist/smithcode-X.Y.Z.tar.gz \
  --title "SmithCode X.Y.Z" \
  --notes-file docs/release-notes-X.Y.Z.md

# 复核：附件与正文
gh release view vX.Y.Z
```

无 `gh` 时用网页端：Releases → Draft a new release → 选标签 `vX.Y.Z` → 标题 `SmithCode X.Y.Z` → 正文粘贴 `docs/release-notes-X.Y.Z.md` → 上传两个产物 → Publish release。

### 约定

- **先推标签再建 Release**：Release 绑定标签，标签未推送时创建的 Release 会在下次推送时冲突
- **正文与 CHANGELOG 不一致时以 release notes 为准**：前者面向使用者、可留白修饰，后者面向开发者、逐条列全
- **已发布的 Release 不重写正文**：需要更正时以新版本修订号补一条，保持历史可追溯（`gh release edit` 仅用于修错别字 / 附件缺失）

## 发布说明

### 0.9.1 — 2026-09-17

本次为**行为调整 + 修复**版本，无破坏性变更。

#### 迭代与目标推进改为默认不设上限

- `[limits].max_iterations`（单次任务最大迭代轮数）默认值由 `30` 改为 `-1`（不限制），对齐 opencode 的 `steps` 缺省「无限迭代」语义：只要模型持续请求工具就继续循环，直到模型给出纯文本回复或你中断
- `[limits].goal_max_turns`（`/goal` 持久目标回合预算）默认值由 `50` 改为 `-1`（不限制）：目标持续自动推进，直到模型核验证据后声明完成 / 受阻，你暂停 / 清除 / 中断，或空转刹车触发
- 需要封顶时仍可显式配置正整数；`--max-iterations N` CLI 参数与 `/goal budget <N>` 继续可用
- `/goal budget` 新增 `unlimited` / `off` / `none` / `-1` 取消上限
- 进度展示随预算自适应：不限时底栏 / 侧边栏 / 状态块只显示回合数（如 `◎ 目标 3`），有预算时显示 `N/M`
- 旧会话快照中保存的旧默认值（30 / 50）不受影响，按快照原值执行

#### 达到迭代上限时改为「强制总结收尾」

此前达到 `max_iterations` 会静默硬中止；现在改为注入收尾提示、以 `tools=None`（不向模型暴露工具）强制模型用纯文本总结：已完成工作、剩余任务、下一步建议。总结正文在流式过程中展示，终端另可见一条「已达上限」警告。

若收尾轮模型仍返回 `tool_calls`，一律剥离不执行，避免会话历史中留下悬空 `tool_call_id`。

#### 修复：LLM 流中途断连自动重试

推理模型思考时，对端（服务商 / 网关 / 代理）可能因空闲超时掐断长连接（表现为 `RemoteProtocolError: incomplete chunked read`）。此前这类**流中途**的传输层错误不在重试范围内，会直接中断任务；现在 `RemoteProtocolError` / `ReadError` / `ReadTimeout` 均纳入瞬时错误重试：

- 每次重试前在终端打印错误详情，方便定位是哪一层断的
- 已输出过正文的流不重试（重放会重复打印）；仅输出过思考内容时可安全重算（思考只展示、不写入会话）
- 重试次数用尽仍失败则照常报错

#### 手动压缩 `/compact` 不再卡界面

此前 `/compact` 在宿主主线程同步执行，摘要请求期间 TUI 整个界面卡死、REPL 也无法输入。现在 `/compact` 改为**后台压缩**：发送后立即提示「正在压缩上下文…」，界面保持可响应，完成后提示「上下文压缩完成。」；压缩期间按 Esc / Ctrl+C 可中断，中断后提示「已取消压缩。」。历史太短、无中段可压时提示「没有可压缩的上下文」。

任务运行中 `/compact` 会被拒绝（与 `/new`、`/sessions` 同属会改写会话状态的命令），避免与进行中的轮次冲突；等待任务结束或按 Esc 中断后再执行。

#### 升级方式

```bash
# uv tool 安装（推荐）
uv tool install --reinstall -e .

# 或从产物安装
pip install dist/smithcode-0.9.1-py3-none-any.whl
```

现有配置无需改动；如需恢复旧行为，在 `~/.smithcode/config.toml` 中显式设置：

```toml
[limits]
max_iterations = 30
goal_max_turns = 50
```

#### 发布产物

| 文件 | 大小 | SHA-256 |
| ---- | ---- | ------- |
| `dist/smithcode-0.9.1-py3-none-any.whl` | 307,542 B | `f54c6196969349a8e5f2de17f68c10c81196616775bbb1f13daaaf4a57db3eee` |
| `dist/smithcode-0.9.1.tar.gz` | 421,564 B | `aceeeab56056fd24a05a8e9ac5abeadf46c6ad3ebf192d298c7d41a342f6a23c` |

本版面向使用者的正文另收录于 `docs/release-notes-0.9.1.md`，可直接作为 GitHub Release 正文（`gh release create --notes-file`）。

校验（在 dist/ 的上级目录执行）：

```bash
sha256sum -c <<'EOF'
f54c6196969349a8e5f2de17f68c10c81196616775bbb1f13daaaf4a57db3eee  dist/smithcode-0.9.1-py3-none-any.whl
aceeeab56056fd24a05a8e9ac5abeadf46c6ad3ebf192d298c7d41a342f6a23c  dist/smithcode-0.9.1.tar.gz
EOF
```

