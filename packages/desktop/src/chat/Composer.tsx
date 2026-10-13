import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Folder, Plus, Square } from "lucide-react";
import { serve, type CommandInfo, type WorkspaceInfo } from "../session/serve";
import { Menu, type MenuEntry } from "../ui/Menu";
import { EffortPicker } from "./EffortPicker";
import { ModelPicker } from "./ModelPicker";
import { PermissionPicker } from "./PermissionPicker";
import { QueuePanel } from "./QueuePanel";

const HISTORY_MAX = 50;

const FALLBACK_COMMANDS: CommandInfo[] = [
  { name: "model", description: "切换模型", usage: "/model [名称]" },
  { name: "effort", description: "调整思考强度", usage: "/effort [档位]" },
  { name: "plan", description: "显示当前任务计划", usage: "/plan" },
  { name: "usage", description: "显示 token 用量", usage: "/usage" },
  { name: "context", description: "显示上下文占用", usage: "/context" },
  { name: "compact", description: "手动压缩上下文", usage: "/compact" },
  { name: "new", description: "开启新会话", usage: "/new [名称]" },
  { name: "rename", description: "重命名当前会话", usage: "/rename <名称>" },
  { name: "sessions", description: "查看/切换历史会话", usage: "/sessions [list]" },
  { name: "skills", description: "查看技能列表", usage: "/skills [refresh]" },
  { name: "mcp", description: "MCP 服务器管理", usage: "/mcp" },
  { name: "help", description: "显示帮助", usage: "/help" },
];

export function Composer({
  sessionId,
  workspace,
  model,
  effort,
  permission,
  busy,
  onSend,
  onStop,
  onPermissionChanged,
  onModelChanged,
  onEffortChanged,
}: {
  sessionId: string | null;
  workspace: WorkspaceInfo | null;
  model: string;
  effort: string;
  permission: string;
  busy: boolean;
  onSend: (text: string) => Promise<void>;
  onStop: () => void;
  onPermissionChanged: (mode: string) => void;
  onModelChanged: (model: string) => void;
  onEffortChanged: (effort: string) => void;
}) {
  const [text, setText] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx] = useState(-1);
  const [commands, setCommands] = useState<CommandInfo[]>(FALLBACK_COMMANDS);
  const [plusOpen, setPlusOpen] = useState(false);
  const [slashIdx, setSlashIdx] = useState(0);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  // 发送守卫：`busy` prop 滞后于 handleSend 的 setBusy（异步），双击 Enter 会发两条
  const sendingRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { commands } = await serve.commands();
        if (!cancelled && commands.length > 0) setCommands(commands);
      } catch {
        // 后端未接入时使用内置命令表
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 208)}px`;
  }, [text]);

  const slashQuery = text.startsWith("/") && !text.includes(" ") ? text.slice(1) : null;
  const slashMatches = useMemo(
    () =>
      slashQuery === null
        ? []
        : commands.filter((c) => c.name.startsWith(slashQuery)).slice(0, 8),
    [slashQuery, commands],
  );
  const slashOpen =
    slashQuery !== null && slashQuery !== dismissedFor && slashMatches.length > 0;

  useEffect(() => {
    setSlashIdx(0);
  }, [slashQuery]);

  async function submit() {
    if (sendingRef.current) return; // 连击守卫：挡同一 tick 内的重复触发
    const value = text.trim();
    if (!value) return;
    // 运行中不拦：后端按 [queue] 配置排队（排队 chip 与取回都在），
    // 拦下来只会变成"点了发送却什么都没发生"。
    sendingRef.current = true;
    setText("");
    setHistIdx(-1);
    setHistory((h) => [value, ...h].slice(0, HISTORY_MAX));
    setPlusOpen(false);
    // 守卫只挡"React 还没把 setText('') 刷进 DOM"这段同步窗口；下一 tick 释放，
    // 否则运行中再发（合法排队）会被拦。不能用 finally——`onSend` 对普通消息
    // 会 await 整轮运行（可达十几分钟）。
    window.setTimeout(() => {
      sendingRef.current = false;
    }, 0);
    try {
      await onSend(value);
    } catch {
      setText(value);
    }
  }

  function complete(name: string) {
    setText(`/${name} `);
    setDismissedFor(null);
    ref.current?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (slashOpen) {
        complete(slashMatches[slashIdx].name);
        return;
      }
      void submit();
      return;
    }
    if (e.key === "Tab" && slashOpen) {
      e.preventDefault();
      complete(slashMatches[slashIdx].name);
      return;
    }
    if (e.key === "Escape") {
      if (slashOpen && slashQuery !== null) {
        setDismissedFor(slashQuery);
        return;
      }
      if (busy) onStop();
      return;
    }
    if (slashOpen) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIdx((i) => (i <= 0 ? slashMatches.length - 1 : i - 1));
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIdx((i) => (i >= slashMatches.length - 1 ? 0 : i + 1));
        return;
      }
    }
    if (e.key === "ArrowUp" && text === "" && history.length > 0) {
      e.preventDefault();
      const next = Math.min(histIdx + 1, history.length - 1);
      setHistIdx(next);
      setText(history[next]);
      return;
    }
    if (e.key === "ArrowDown" && histIdx >= 0) {
      e.preventDefault();
      const next = histIdx - 1;
      setHistIdx(next);
      setText(next >= 0 ? history[next] : "");
    }
  }

  const plusItems: MenuEntry[] = [
    { heading: "斜杠命令" },
    ...commands.map((c) => ({
      label: `/${c.name}`,
      hint: c.description,
      onSelect: () => complete(c.name),
    })),
  ];

  return (
    <div className="composer-wrap">
      <div className="composer-inner">
        {sessionId ? (
          <QueuePanel
            sessionId={sessionId}
            onTakeBack={(queued) => {
              // 取回编辑：拼到现有草稿之后（有草稿就换行分隔），光标到末尾
              setText((cur) => (cur.trim() ? `${cur.replace(/\n$/, "")}\n${queued}` : queued));
              setDismissedFor(null);
              requestAnimationFrame(() => {
                const el = ref.current;
                if (el) el.selectionStart = el.selectionEnd = el.value.length;
                el?.focus();
              });
            }}
          />
        ) : null}
        <div className="composer">
          {slashOpen ? (
            <div className="slash-menu" role="listbox" aria-label="命令补全">
              {slashMatches.map((c, i) => (
                <button
                  key={c.name}
                  role="option"
                  aria-selected={i === slashIdx}
                  className={`slash-item${i === slashIdx ? " active" : ""}`}
                  onMouseEnter={() => setSlashIdx(i)}
                  onClick={() => complete(c.name)}
                >
                  <span className="cmd">/{c.name}</span>
                  <span className="desc">{c.description}</span>
                </button>
              ))}
            </div>
          ) : null}
          <span className="composer-chip">
            <Folder size={12} />
            {workspace?.name ?? "工作区"}
          </span>
          <textarea
            ref={ref}
            value={text}
            rows={1}
            placeholder="做点什么…（Enter 发送，/ 使用命令）"
            aria-label="输入任务"
            onChange={(e) => {
              setText(e.target.value);
              setDismissedFor(null);
            }}
            onKeyDown={onKeyDown}
          />
          <div className="composer-tools">
            <span className="popover-anchor">
              <button
                className="icon"
                title="命令与技能"
                aria-label="命令与技能"
                onClick={() => setPlusOpen(!plusOpen)}
              >
                <Plus size={16} />
              </button>
              {plusOpen ? (
                <Menu up items={plusItems} onClose={() => setPlusOpen(false)} />
              ) : null}
            </span>
            <PermissionPicker
              sessionId={sessionId}
              mode={permission}
              onChanged={onPermissionChanged}
            />
            <span className="spacer" />
            <ModelPicker
              sessionId={sessionId}
              model={model}
              onChanged={onModelChanged}
            />
            <EffortPicker
              sessionId={sessionId}
              effort={effort}
              onChanged={onEffortChanged}
            />
            {busy && !text.trim() ? (
              <button className="send-btn stop" title="停止（Esc）" onClick={onStop}>
                <Square size={13} />
              </button>
            ) : (
              <button
                className="send-btn"
                title={busy ? "发送（任务运行中，将排队）" : "发送（Enter）"}
                disabled={!text.trim()}
                onClick={() => void submit()}
              >
                <ArrowUp size={16} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
