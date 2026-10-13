import { useEffect, useState } from "react";
import { Loader2, Plug, RefreshCw } from "lucide-react";
import { serve, type McpServerStatus } from "../session/serve";

/**
 * 命令行 → argv：支持单/双引号包裹（`--path "/my dir"`）与反斜杠转义。
 *
 * 朴素的 `split(/\s+/)` 会把带空格的参数拆碎——用户以为传的是一个路径，
 * 实际变成两个参数，配出来的 server 行为完全不对。这不是 shell 解析：
 * 不做变量展开 / 管道 / 重定向，只做引号分组。
 */
function splitArgv(input: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quote: '"' | "'" | null = null;
  let has = false; // 当前是否已累积字符（区分 `""` 空参数与"没参数"）
  for (let i = 0; i < input.length; i++) {
    const ch = input[i];
    if (quote) {
      if (ch === "\\" && quote === '"' && i + 1 < input.length) {
        cur += input[++i]; // 双引号内 `\"` 还原成字面量
      } else if (ch === quote) {
        quote = null;
      } else {
        cur += ch;
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
      continue;
    }
    if (ch === "\\" && i + 1 < input.length) {
      cur += input[++i];
      has = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (has) {
        out.push(cur);
        cur = "";
        has = false;
      }
      continue;
    }
    cur += ch;
    has = true;
  }
  if (has) out.push(cur);
  return out;
}

export function MCPPanel() {
  const [servers, setServers] = useState<McpServerStatus[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [detail, setDetail] = useState<{ name: string; kind: string; text: string } | null>(null);
  const [confirmRemove, setConfirmRemove] = useState("");
  const [adding, setAdding] = useState(false);

  async function refresh(showSpinner = false) {
    if (showSpinner) setLoading(true);
    try {
      const { servers } = await serve.mcpServers();
      setServers(servers);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, []);

  async function act(name: string, action: "enable" | "disable" | "reconnect" | "auth" | "remove") {
    setBusy(`${name}:${action}`);
    setError("");
    try {
      await serve.mcpAction(name, action);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  }

  async function showLogs(name: string) {
    try {
      const { logs } = await serve.mcpLogs(name);
      setDetail({ name, kind: "日志", text: logs || "（暂无日志）" });
    } catch (e) {
      setError(String(e));
    }
  }

  async function showTools(name: string) {
    try {
      const { tools } = await serve.mcpTools(name);
      setDetail({
        name,
        kind: "工具",
        text:
          tools
            .map((t) => `${t.exposed}${t.read_only ? "（只读）" : ""}\n  ${t.description}`)
            .join("\n") || "（无工具）",
      });
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div>
      <h2 className="settings-section-title">MCP 服务器</h2>
      <p className="settings-section-hint">
        插件化工具服务器：用户级配置在 config.toml，项目级在 .smithcode/mcp.json；密钥只写 {"${VAR}"} 引用。
      </p>
      <div className="modal-options" style={{ marginTop: 0 }}>
        <button className="primary" onClick={() => setAdding(!adding)}>
          {adding ? "收起添加表单" : "添加服务器"}
        </button>
        <button onClick={() => void refresh(true)} disabled={loading}>
          {loading ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} 刷新
        </button>
      </div>
      {adding ? <AddForm onDone={() => { setAdding(false); void refresh(); }} onError={setError} /> : null}
      {error ? <div className="settings-err">{error}</div> : null}

      <div className="list-panel" style={{ marginTop: 14 }}>
        {servers.length === 0 ? <div className="pane-empty">还没有配置 MCP 服务器。</div> : null}
        {servers.map((s) => {
          const disabled = s.state === "disabled";
          return (
            <div key={s.name} className="list-row">
              <span className={`state-dot ${s.state}`} title={s.state_label} />
              <div className="grow">
                <div className="title">
                  <Plug size={13} />
                  {s.name}
                  <span className="tag">{s.scope_label}</span>
                  <span className="tag">{s.transport}</span>
                  {s.tool_count > 0 ? <span className="tag">{s.tool_count} 工具</span> : null}
                  <span className={`tag${s.state === "failed" || s.state === "missing_env" ? " err" : s.state === "needs_auth" ? " warn" : ""}`}>
                    {s.state_label}
                  </span>
                </div>
                <div className="desc" title={s.error || s.target}>
                  {s.error || s.target}
                </div>
              </div>
              <div className="actions">
                {s.state === "needs_auth" ? (
                  <button onClick={() => void act(s.name, "auth")} disabled={busy !== ""}>
                    授权
                  </button>
                ) : null}
                {!disabled ? (
                  <button onClick={() => void act(s.name, "reconnect")} disabled={busy !== ""}>
                    重连
                  </button>
                ) : null}
                <button
                  onClick={() => void act(s.name, disabled ? "enable" : "disable")}
                  disabled={busy !== ""}
                >
                  {disabled ? "启用" : "停用"}
                </button>
                <button onClick={() => void showTools(s.name)}>工具</button>
                <button onClick={() => void showLogs(s.name)}>日志</button>
                <button
                  className={confirmRemove === s.name ? "danger" : ""}
                  onClick={() => {
                    if (confirmRemove === s.name) {
                      setConfirmRemove("");
                      void act(s.name, "remove");
                    } else {
                      setConfirmRemove(s.name);
                      setTimeout(() => setConfirmRemove((v) => (v === s.name ? "" : v)), 4000);
                    }
                  }}
                  disabled={busy !== ""}
                >
                  {confirmRemove === s.name ? "确认删除" : "删除"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {detail ? (
        <div>
          <div className="settings-row" style={{ marginTop: 12 }}>
            <strong style={{ fontSize: 13 }}>
              {detail.name} · {detail.kind}
            </strong>
            <button className="ghost" onClick={() => setDetail(null)}>
              收起
            </button>
          </div>
          <pre className="log-box">{detail.text}</pre>
        </div>
      ) : null}
    </div>
  );
}

function AddForm({ onDone, onError }: { onDone: () => void; onError: (msg: string) => void }) {
  const [name, setName] = useState("");
  const [type, setType] = useState("stdio");
  const [target, setTarget] = useState("");
  const [scope, setScope] = useState("user");
  const [oauth, setOauth] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit() {
    const trimmed = target.trim();
    if (!name.trim() || !trimmed) {
      onError("名称与命令/地址都要填");
      return;
    }
    setBusy(true);
    try {
      const body: Parameters<typeof serve.mcpAdd>[0] = {
        name: name.trim(),
        type,
        scope,
      };
      if (type === "stdio") body.command = splitArgv(trimmed);
      else {
        body.url = trimmed;
        body.oauth = oauth;
      }
      await serve.mcpAdd(body);
      onDone();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="settings-field">
        <span className="label">名称</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如 filesystem" />
      </div>
      <div className="settings-field">
        <span className="label">类型</span>
        <select value={type} onChange={(e) => setType(e.target.value)}>
          <option value="stdio">stdio（本地命令）</option>
          <option value="http">http（Streamable HTTP）</option>
          <option value="sse">sse</option>
        </select>
      </div>
      <div className="settings-field">
        <span className="label">{type === "stdio" ? "命令（空格分隔 argv）" : "地址"}</span>
        <input
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder={type === "stdio" ? "npx -y @modelcontextprotocol/server-filesystem /tmp" : "https://…"}
        />
      </div>
      <div className="settings-field">
        <span className="label">作用域</span>
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          <option value="user">用户级（~/.smithcode/config.toml）</option>
          <option value="project">项目级（.smithcode/mcp.json，会进版本库）</option>
        </select>
      </div>
      {type !== "stdio" ? (
        <label className="settings-row" style={{ fontSize: 13 }}>
          <input type="checkbox" checked={oauth} onChange={(e) => setOauth(e.target.checked)} />
          使用 OAuth 授权
        </label>
      ) : null}
      <p className="settings-section-hint" style={{ marginTop: 10 }}>
        带密钥的服务器请把值写成 {"${VAR}"} 引用（在终端用 /mcp add 向导可录入密钥）。
      </p>
      <div className="modal-options">
        <button className="primary" onClick={() => void submit()} disabled={busy}>
          {busy ? "添加中…" : "添加"}
        </button>
      </div>
    </div>
  );
}
