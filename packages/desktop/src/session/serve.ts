const DEFAULT_PORT = 18789;

// 鉴权 token 只活在内存里（模块变量），绝不进 localStorage：
// localStorage 里任何 JS 都能读（XSS/恶意 MCP 输出/调试片段），
// 等于把完整权限的 bearer 交出去。Tauri 下经 invoke 从壳里取，
// dev 下经 BootScreen 手填——两者都只进这个变量。
let memToken = "";
let memPort = "";

export class ServeError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ServeError";
    this.status = status;
  }

  /** 展示用：界面一律 `String(e)`，别把类名（`ServeError: `）糊到用户脸上。 */
  toString(): string {
    return this.message;
  }
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function readStoredPort(): string {
  try {
    return localStorage.getItem("smith.port") ?? String(DEFAULT_PORT);
  } catch {
    return String(DEFAULT_PORT);
  }
}

async function tauriEndpoint(): Promise<{ port: number; token: string } | null> {
  if (!isTauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const port = (await invoke("sidecar_port")) as number;
    const token = (await invoke("sidecar_token")) as string;
    if (port) {
      try {
        localStorage.setItem("smith.port", String(port));
      } catch {
        // 无痕模式等：端口记不住，下次重取即可
      }
      // token 只进内存：localStorage 对页面里任何 JS 可读
      if (token) memToken = token;
      memPort = String(port);
      return { port, token };
    }
  } catch {
    // 非 Tauri / 壳未就绪时回退内存 + localStorage 端口（pnpm dev 直连 serve）
  }
  return null;
}

let endpointReady: Promise<void> | null = null;

async function fetchEndpoint(): Promise<void> {
  await tauriEndpoint().catch(() => {});
  if (!memPort) memPort = readStoredPort();
  if (memToken) return;
  // dev 直连（非 Tauri / 壳还没给出 token）：按 BootScreen 的提示读手填 token。
  // 只读不写——Tauri 下的 token 仍然只活在内存里，落盘的那份是 dev 模式自己填的。
  try {
    memToken = localStorage.getItem("smith.token") ?? "";
  } catch {
    // 无痕模式等：读不到就等 BootScreen 提示
  }
}

export function ensureEndpoint(): Promise<void> {
  if (!endpointReady) endpointReady = fetchEndpoint();
  return endpointReady;
}

/** 重新向 Tauri 壳取 port/token：壳拉起 serve 是异步的，首次可能还没解析出 token。 */
export function refreshEndpoint(): Promise<void> {
  endpointReady = fetchEndpoint();
  return endpointReady;
}

/** BootScreen 手填 token（dev 直连用）：只进内存。 */
export function setManualToken(tk: string) {
  memToken = tk;
}

export function getToken(): string {
  return memToken;
}

function baseUrl(): string {
  const port = memPort || readStoredPort();
  return `http://127.0.0.1:${port}`;
}

function token(): string {
  return memToken;
}

async function request<T>(method: string, path: string, body?: unknown, opts?: { timeout?: number }): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const tk = token();
  if (tk) headers["Authorization"] = `Bearer ${tk}`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts?.timeout ?? 15000);
  let resp: Response;
  try {
    resp = await fetch(baseUrl() + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ServeError(0, "请求超时：sidecar 无响应");
    }
    throw new ServeError(0, `网络错误：${e}`);
  } finally {
    clearTimeout(timer);
  }
  const data = (await resp.json().catch(() => ({}))) as T & { error?: string };
  if (!resp.ok) {
    throw new ServeError(resp.status, data.error ?? `HTTP ${resp.status}`);
  }
  if (data && typeof data === "object" && "error" in data && data.error) {
    throw new ServeError(resp.status, String(data.error));
  }
  return data;
}

export interface OnlineSession {
  id: string;
  busy: boolean;
  pending: number;
  permission?: string;
  model?: string;
  effort?: string;
}

export interface HistorySession {
  id: string;
  title: string;
  updated: number;
  created: number;
  cwd: string;
  model: string;
  first_prompt: string;
}

export interface WorkspaceInfo {
  cwd: string;
  name: string;
  extra: string[];
}

export interface CommandInfo {
  name: string;
  description: string;
  usage: string;
}

export interface DirEntry {
  name: string;
  type: "dir" | "file";
  size: number;
}

export interface PendingPrompt {
  id: string;
  session_id: string;
  kind: string;
  title: string;
  created: number;
  detail: string[];
  options: string[];
  payload: Record<string, unknown> | null;
}

export interface PromptResult {
  queued: boolean;
  status?: string;
  text?: string;
  timeout?: boolean;
}

export interface DispatchResult {
  text: string | null;
  kind: string;
  select?: {
    title: string;
    command: string;
    readonly?: boolean;
    items: {
      label: string;
      value: string;
      current: boolean;
      description?: string;
      trailing?: string;
    }[];
  };
  wizard?: { name: string; payload: Record<string, unknown> };
  session_reset?: boolean;
  session_resume?: boolean;
  refresh_status?: boolean;
  skipped_busy?: boolean;
  start_compact?: boolean;
}

export interface ProviderSnapshot {
  url: string;
  model: string;
  effort: string;
  key_configured: boolean;
  budget: number;
}

export interface McpServerStatus {
  name: string;
  scope: string;
  scope_label: string;
  state: string;
  state_label: string;
  transport: string;
  target: string;
  url: string;
  oauth: boolean;
  tool_count: number;
  error: string;
  missing: string[];
}

export interface McpToolInfo {
  original: string;
  exposed: string;
  description: string;
  read_only: boolean;
}

export interface SkillInfo {
  name: string;
  description: string;
  scope: string;
  location: string;
  active: boolean;
  disabled: boolean;
  disabled_by: string;
  model_invocable: boolean;
  warnings: string[];
  resources: string[];
  body: string;
}

export interface SkillsPayload {
  loaded: boolean;
  skills: SkillInfo[];
  diagnostics: string[];
}

export interface ProjectEntry {
  slug: string;
  cwd: string;
  name: string;
  sessionCount: number;
  updated: number;
}

export interface HistoryQuery {
  /** 项目 slug：不传 = serve 当前默认项目；`all` 跨项目查全 */
  project?: string;
  all?: boolean;
  limit?: number;
}

export const serve = {
  ensureEndpoint,
  refreshEndpoint,
  setManualToken,
  getToken,
  setEndpoint(port: number, tk: string) {
    // 兼容旧调用：端口仍可持久化，token 只进内存
    try {
      localStorage.setItem("smith.port", String(port));
    } catch {
      // 忽略
    }
    memPort = String(port);
    if (tk) memToken = tk;
  },
  rotateToken(tk: string) {
    memToken = tk;
    endpointReady = null;
  },
  health: () => request<{ ok: boolean; version?: string }>("GET", "/health", undefined, { timeout: 5000 }),
  workspace: () => request<WorkspaceInfo>("GET", "/workspace"),
  listProjects: () => request<{ projects: ProjectEntry[] }>("GET", "/projects"),
  switchWorkspace: (path: string) =>
    request<{ ok: boolean }>("POST", "/workspace", { path }),
  registerProject: (path: string) => request<ProjectEntry>("POST", "/projects", { path }),
  getWorkspace: (sessionId: string) =>
    request<WorkspaceInfo>("GET", `/sessions/${sessionId}/workspace`),
  commands: () => request<{ commands: CommandInfo[] }>("GET", "/commands"),
  listOnline: () => request<{ sessions: OnlineSession[] }>("GET", "/sessions"),
  listHistory: (q?: HistoryQuery) => {
    const params = new URLSearchParams();
    if (q?.project) params.set("project", q.project);
    if (q?.all) params.set("all", "1");
    if (q?.limit) params.set("limit", String(q.limit));
    const qs = params.toString();
    return request<{ sessions: HistorySession[] }>("GET", `/sessions/history${qs ? `?${qs}` : ""}`);
  },
  create: (resume?: string, opts?: { workspace?: string }) =>
    request<{ id: string }>(
      "POST",
      "/sessions",
      { ...(resume ? { resume } : {}), ...(opts?.workspace ? { workspace: opts.workspace } : {}) },
    ),
  drop: (id: string) => request<{ ok: boolean }>("DELETE", `/sessions/${id}`),
  purge: (id: string) => request<{ ok: boolean }>("POST", `/sessions/${id}/purge`, {}),
  rename: (id: string, title: string) =>
    request<{ ok: boolean }>("POST", `/sessions/${id}/rename`, { title }),
  pending: (id: string) => request<{ pending: PendingPrompt[] }>("GET", `/sessions/${id}/pending`),
  prompt: (id: string, text: string) =>
    request<PromptResult>("POST", `/sessions/${id}/prompt`, { text }, { timeout: 1000 * 60 * 11 }),
  promptAsync: (id: string, text: string) =>
    request<PromptResult>("POST", `/sessions/${id}/prompt_async`, { text }),
  takeQueued: (id: string, itemId: string) =>
    request<{ id: string; text: string }>("POST", `/sessions/${id}/queue/take`, { id: itemId }),
  cancelQueued: (id: string, itemId: string) =>
    request<{ ok: boolean }>("POST", `/sessions/${id}/queue/cancel`, { id: itemId }),
  issueTicket: () => request<{ ticket: string }>("POST", "/events/ticket", {}),
  rotateAuth: () => request<{ token: string }>("POST", "/auth/rotate", {}),
  interrupt: (id: string) => request<{ ok: boolean }>("POST", `/sessions/${id}/interrupt`, {}),
  permission: (id: string, mode: string) =>
    request<{ mode: string }>("POST", `/sessions/${id}/permission`, { mode }),
  dispatch: (id: string, text: string) =>
    request<DispatchResult>("POST", `/sessions/${id}/dispatch`, { text }),
  reply: (sid: string, pid: string, answer: { outcome: string; value?: string; values?: string[] }) =>
    request<{ ok: boolean }>("POST", `/sessions/${sid}/prompts/${pid}/reply`, answer),
  settings: () => request<ProviderSnapshot>("POST", "/settings", {}),
  reloadSettings: () =>
    request<{ changed: Record<string, [unknown, unknown]> }>("POST", "/settings/reload", {}),
  applyProvider: (body: { url: string; model: string; key: string; budget: number }) =>
    request<{ changed: Record<string, [unknown, unknown]> }>(
      "POST",
      "/settings/provider",
      body,
    ),
  listDir: (path: string, sessionId?: string | null) =>
    request<{ path: string; entries: DirEntry[]; truncated: boolean }>(
      "GET",
      // 会话级：按该会话的沙箱根解析（多项目下不串目录）；无会话时回退进程默认
      `${sessionId ? `/sessions/${sessionId}/fs/list` : "/fs/list"}?path=${encodeURIComponent(path)}`,
    ),
  readFile: (path: string, sessionId?: string | null) =>
    request<{ content: string; truncated: boolean; size: number }>(
      "GET",
      `${sessionId ? `/sessions/${sessionId}/fs/read` : "/fs/read"}?path=${encodeURIComponent(path)}`,
    ),
  mcpServers: () => request<{ servers: McpServerStatus[] }>("GET", "/mcp/servers"),
  mcpAction: (name: string, action: "enable" | "disable" | "reconnect" | "auth" | "remove") =>
    request<{ ok: boolean }>(
      "POST",
      `/mcp/servers/${encodeURIComponent(name)}/${action}`,
      {},
    ),
  mcpLogs: (name: string, lines = 20) =>
    request<{ logs: string }>(
      "GET",
      `/mcp/servers/${encodeURIComponent(name)}/logs?lines=${lines}`,
    ),
  mcpTools: (name: string) =>
    request<{ tools: McpToolInfo[] }>("GET", `/mcp/servers/${encodeURIComponent(name)}/tools`),
  mcpAdd: (body: {
    name: string;
    type: string;
    scope: string;
    command?: string[];
    url?: string;
    oauth?: boolean;
  }) => request<{ ok: boolean; server: McpServerStatus }>("POST", "/mcp/servers", body),
  skills: (sessionId: string) =>
    request<SkillsPayload>("GET", `/sessions/${sessionId}/skills`),
};
