import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { reduceChatItems, textOf } from "../chat/chatItems";
import { Composer } from "../chat/Composer";
import { EmptyState } from "../chat/EmptyState";
import { MessageList } from "../chat/MessageList";
import { AskUserModal } from "../modals/AskUserModal";
import { PermissionModal } from "../modals/PermissionModal";
import { SelectModal, type SelectState } from "../modals/SelectModal";
import { hasDiffs } from "../panes/DiffPane";
import type { SettingsSection } from "../settings/sections";
import { SettingsDialog } from "../settings/SettingsDialog";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { ChatTopActions } from "../shell/ChatTopActions";
import { OpenDirDialog } from "../shell/OpenDirDialog";
import { RenameDialog } from "../shell/RenameDialog";
import { RightSidebar, type RightPaneKind, type SideWindow } from "../shell/RightSidebar";
import { SearchDialog } from "../shell/SearchDialog";
import { Sidebar, type ProjectExpand, type ThreadRow } from "../shell/Sidebar";
import type { SmithEnvelope } from "../session/protocol";
import { dataOf } from "../session/protocol";
import {
  isTauri,
  serve,
  ServeError,
  type HistorySession,
  type PendingPrompt,
  type ProjectEntry,
  type WorkspaceInfo,
} from "../session/serve";
import { isDraft, useSessionStore } from "../session/sessionStore";
import { useSse } from "../session/useSse";
import { BootScreen } from "./BootScreen";
import { listenMenu, pickDirectory, setWindowTitle } from "./desktop";
import { useTheme } from "./useTheme";

const HISTORY_REFRESH_MS = 10_000;

export function App() {
  const { mode, setMode } = useTheme();
  const tabs = useSessionStore((s) => s.tabs);
  const activeId = useSessionStore((s) => s.activeId);
  const sidebarOpen = useSessionStore((s) => s.sidebarOpen);
  const setSidebarOpen = useSessionStore((s) => s.setSidebarOpen);
  const rightOpen = useSessionStore((s) => s.rightOpen);
  const setRightOpen = useSessionStore((s) => s.setRightOpen);
  const addTab = useSessionStore((s) => s.addTab);
  const addDraftTab = useSessionStore((s) => s.addDraftTab);
  const promoteTab = useSessionStore((s) => s.promoteTab);
  const pushPendingUser = useSessionStore((s) => s.pushPendingUser);
  const removePendingUser = useSessionStore((s) => s.removePendingUser);
  const resolvePendingUser = useSessionStore((s) => s.resolvePendingUser);
  const closeTab = useSessionStore((s) => s.closeTab);
  const renameTab = useSessionStore((s) => s.renameTab);
  const pushEvent = useSessionStore((s) => s.pushEvent);
  const setBusy = useSessionStore((s) => s.setBusy);
  const setPendingCount = useSessionStore((s) => s.setPendingCount);
  const setModelEffort = useSessionStore((s) => s.setModelEffort);
  const setPermission = useSessionStore((s) => s.setPermission);
  const clearEvents = useSessionStore((s) => s.clearEvents);

  const [sidecarOk, setSidecarOk] = useState<boolean | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [projects, setProjects] = useState<ProjectEntry[]>([]);
  /** 项目列表首屏是否已尝试拉取：区分侧栏的「加载中…」与「真没有项目」 */
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  /** 当前项目（slug）：历史列表的过滤条件 + 新会话的落点。 */
  const [activeProject, setActiveProjectState] = useState<string | null>(null);
  const activeProjectRef = useRef<string | null>(null);
  const setActiveProject = useCallback((slug: string | null) => {
    activeProjectRef.current = slug;
    setActiveProjectState(slug);
  }, []);
  const [history, setHistory] = useState<HistorySession[]>([]);
  /** 项目文件夹展开态：展开的项目 + 各自按需拉取的会话（侧栏只做展示） */
  const [expand, setExpand] = useState<ProjectExpand>({
    open: [],
    rows: {},
    loading: [],
    error: "",
    showAll: [],
  });
  const expandRef = useRef(expand);
  /** 展开态请求号：按 slug 计数，只有最后一次回包算数 */
  const expandReq = useRef<Record<string, number>>({});

  useEffect(() => {
    expandRef.current = expand;
  }, [expand]);
  const [pendings, setPendings] = useState<PendingPrompt[]>([]);
  const [selectState, setSelectState] = useState<SelectState | null>(null);
  const [sideWindows, setSideWindows] = useState<SideWindow[]>([]);
  const [sideActive, setSideActive] = useState<number | null>(null);
  const sideSeq = useRef(0);
  const sideActiveRef = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const [sideClosing, setSideClosing] = useState(false);

  useEffect(() => {
    sideActiveRef.current = sideActive;
  }, [sideActive]);

  useEffect(
    () => () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    },
    [],
  );
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  /** 搜索是跨项目的：`history` 只含当前项目，Ctrl+K 要另开一路全量查询。 */
  const [searchHistory, setSearchHistory] = useState<HistorySession[]>([]);
  const [renameTarget, setRenameTarget] = useState<{ id: string; title: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; title: string } | null>(null);
  const [openDirOpen, setOpenDirOpen] = useState(false);
  const [appError, setAppError] = useState("");
  const lastProjects = useRef(0);
  const lastHistory = useRef(0);
  /** 历史请求序号：连点 / 切项目时旧请求的回包不得覆盖新回包。 */
  const historyReq = useRef(0);

  const active = tabs.find((t) => t.sessionId === activeId) ?? null;

  const notice = useCallback(
    (sessionId: string, level: string, text: string) => {
      pushEvent(sessionId, {
        id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        type: "session.notice",
        version: 1,
        created: Date.now() / 1000,
        session_id: sessionId,
        durable: false,
        seq: null,
        data: { text, level },
      });
    },
    [pushEvent],
  );

  const refreshProjects = useCallback(async (force = false) => {
    const now = Date.now();
    if (!force && now - lastProjects.current < HISTORY_REFRESH_MS) return;
    lastProjects.current = now;
    try {
      const { projects: list } = await serve.listProjects();
      setProjects(list);
    } catch (e) {
      // 老 sidecar 没实现 /projects、后端不可用时静默降级（沿用原语义）；
      // 401 必须出声：那是 token 没同步，不说话的话侧栏永远空着
      if (e instanceof ServeError && e.status === 401) {
        setAppError("本地服务未授权（token 尚未同步）：稍后自动重试，也可点项目区的刷新按钮");
      }
    }
  }, []);

  /** 拉某个项目的会话：展开文件夹、切到该项目、会话列表刷新时都会走这里。 */
  const loadProjectHistory = useCallback(async (slug: string) => {
    const req = (expandReq.current[slug] ?? 0) + 1;
    expandReq.current[slug] = req;
    setExpand((prev) =>
      prev.loading.includes(slug)
        ? { ...prev, error: "" }
        : { ...prev, loading: [...prev.loading, slug], error: "" },
    );
    try {
      const { sessions } = await serve.listHistory({ project: slug, limit: 200 });
      if (expandReq.current[slug] !== req) return;
      setExpand((prev) => ({
        ...prev,
        rows: { ...prev.rows, [slug]: sessions },
        loading: prev.loading.filter((s) => s !== slug),
      }));
    } catch {
      if (expandReq.current[slug] !== req) return;
      setExpand((prev) => ({
        ...prev,
        loading: prev.loading.filter((s) => s !== slug),
        error: "无法加载该目录的会话",
      }));
    }
  }, []);

  /** 展开 / 收起一个项目文件夹；展开时顺带拉取该目录的会话。 */
  const toggleProjectExpand = useCallback(
    (slug: string) => {
      const willOpen = !expand.open.includes(slug);
      setExpand((prev) => ({
        ...prev,
        open: willOpen ? [...prev.open, slug] : prev.open.filter((s) => s !== slug),
        showAll: willOpen ? prev.showAll : prev.showAll.filter((s) => s !== slug),
        error: "",
      }));
      if (willOpen) void loadProjectHistory(slug);
    },
    [expand.open, loadProjectHistory],
  );

  /** 「还有 N 个会话」标签：在「只看 6 条」与「全部」之间切换。 */
  const toggleProjectShowAll = useCallback((slug: string) => {
    setExpand((prev) => ({
      ...prev,
      showAll: prev.showAll.includes(slug)
        ? prev.showAll.filter((s) => s !== slug)
        : [...prev.showAll, slug],
    }));
  }, []);

  const refreshHistory = useCallback(async (force = false) => {
    const now = Date.now();
    if (!force && now - lastHistory.current < HISTORY_REFRESH_MS) return;
    lastHistory.current = now;
    const req = ++historyReq.current;
    const slug = activeProjectRef.current;
    try {
      const { sessions } = await serve.listHistory(slug ? { project: slug } : undefined);
      if (req !== historyReq.current) return;
      setHistory(sessions);
    } catch {
      // 全局历史只是搜索框的兜底，失败不打扰用户
    }
    // 已展开的项目顺带刷新：新建 / 删除会话后文件夹里要立刻看得到
    for (const s of expandRef.current.open) void loadProjectHistory(s);
  }, [loadProjectHistory]);

  /**
   * 挂起询问按会话合并（`pendings` 里每条自带 `session_id`）：
   * - 无参 = 刷全部打开的会话（挂载 / 断线重连 / tab 增减）；
   * - 带 sessionId = 只刷那一个（SSE 上 `prompt.started/finished` 指名道姓，
   *   后台 tab 里有询问在等也能立刻亮出角标，切过去就能答）。
   * 刷成功的会话整段替换，失败的保留旧值（sidecar 没起来时不要把状态抹掉）。
   */
  const openSessionIds = tabs
    .filter((t) => !isDraft(t.sessionId))
    .map((t) => t.sessionId)
    .join(",");

  const refreshPending = useCallback(
    async (sessionId?: string) => {
      const ids = sessionId ? [sessionId] : openSessionIds.split(",").filter(Boolean);
      if (ids.length === 0) return;
      const results = await Promise.all(
        ids.map(async (id) => {
          try {
            const { pending } = await serve.pending(id);
            return { id, pending };
          } catch {
            return null; // sidecar 未起 / 会话已删：这一路的旧值留着
          }
        }),
      );
      const ok = results.filter(
        (r): r is { id: string; pending: PendingPrompt[] } => r !== null,
      );
      if (ok.length === 0) return;
      const okIds = new Set(ok.map((r) => r.id));
      setPendings((prev) => [
        ...prev.filter((p) => !okIds.has(p.session_id)),
        ...ok.flatMap((r) => r.pending),
      ]);
      for (const r of ok) setPendingCount(r.id, r.pending.length);
    },
    [openSessionIds, setPendingCount],
  );

  /**
   * 以服务端为准校正打开 tab 的 busy：结束事件可能因 SSE 断线丢失，不校正的话
   * 停止按钮会一直亮着，点 interrupt 也只是空转（看起来就是「按了没反应」）。
   * 断线重连与点停止之后都补一次。
   */
  const resyncBusy = useCallback(async () => {
    try {
      const { sessions } = await serve.listOnline();
      const busyById = new Map(sessions.map((s) => [s.id, s.busy]));
      const st = useSessionStore.getState();
      for (const t of st.tabs) {
        if (isDraft(t.sessionId)) continue;
        const b = busyById.get(t.sessionId);
        if (typeof b === "boolean" && t.busy !== b) st.setBusy(t.sessionId, b);
      }
    } catch {
      // sidecar 不可达：保持现状，SSE 重连后会再试
    }
  }, []);

  // 关掉的 tab 不再留着它的挂起询问：角标随 tab 没了，弹窗也不该被切回来时复活
  useEffect(() => {
    setPendings((prev) => {
      const open = new Set(tabs.map((t) => t.sessionId));
      const next = prev.filter((p) => open.has(p.session_id));
      return next.length === prev.length ? prev : next;
    });
  }, [tabs]);

  const onEvent = useCallback(
    (env: SmithEnvelope) => {
      if (!env.session_id) return;
      pushEvent(env.session_id, env);
      if (env.type === "session.message.ended") {
        // 服务端记下这条 user 消息了：摘掉发送时垫的本地回显
        const msg = dataOf(env, "session.message.ended");
        const m = msg?.message as { role?: string; content?: unknown } | undefined;
        if (m?.role === "user") resolvePendingUser(env.session_id, textOf(m.content));
      }
      if (env.type === "session.execution.started") setBusy(env.session_id, true);
      if (
        env.type === "session.execution.succeeded" ||
        env.type === "session.execution.failed" ||
        env.type === "session.execution.interrupted" ||
        env.type === "session.idle"
      ) {
        setBusy(env.session_id, false);
        void refreshHistory();
      }
      if (env.type === "session.title.changed") {
        const data = dataOf(env, "session.title.changed");
        if (data?.title) {
          renameTab(env.session_id, data.title);
          void refreshHistory();
        }
      }
      if (env.type === "session.model.selected") {
        const data = dataOf(env, "session.model.selected");
        if (data) setModelEffort(env.session_id, data.model ?? "", data.effort ?? "");
      }
    },
    [pushEvent, resolvePendingUser, setBusy, renameTab, setModelEffort, refreshHistory],
  );

  // 草稿 tab 没有服务端会话，不能进 SSE 订阅（否则整条流会被 404 掐掉）
  useSse(
    tabs.filter((t) => !isDraft(t.sessionId)).map((t) => t.sessionId),
    onEvent,
    refreshPending,
    resyncBusy,
  );

  // 连接：端口/token → 一次 health。成功进主界面，失败交给 BootScreen 轮询
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await serve.ensureEndpoint();
      } catch {
        // 非 Tauri 环境：直接用 localStorage 的端口/token
      }
      if (cancelled) return;
      try {
        await serve.health();
        if (!cancelled) setSidecarOk(true);
      } catch {
        if (!cancelled) setSidecarOk(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 首屏数据挂在「sidecar 就绪」上，而不是上面那次 health 之后：
  // 冷启动时首次 health 常因 sidecar 还没 listen 而失败 → 走 BootScreen，
  // 那条路上面的 effect 已经 return 了；若数据加载还留在它里面，BootScreen
  // 成功进来后 workspace/projects/history 永远不会拉，侧栏就一直是个空占位。
  useEffect(() => {
    if (sidecarOk !== true) return;
    let cancelled = false;
    (async () => {
      // /health 是免鉴权的（serve.py 在 _auth 之前就返回），而 token 由壳异步
      // 从 sidecar stdout 的 token= 行解析——先用一次带鉴权的请求确认 token
      // 已就位再拉数据，否则 401 会被各处的 catch 静默吞掉
      for (let i = 0; i < 40 && !cancelled; i += 1) {
        try {
          await serve.refreshEndpoint();
        } catch {
          // 非 Tauri：退回 localStorage 的端口/token
        }
        try {
          await serve.listOnline();
          break;
        } catch {
          await new Promise((r) => setTimeout(r, 500));
        }
      }
      if (cancelled) return;
      await Promise.all([
        serve
          .workspace()
          .then((info) => {
            if (!cancelled) setWorkspace(info);
          })
          .catch(() => {
            // 工作区信息不可用时留空（空状态显示通用文案）
          }),
        refreshProjects(true),
        refreshHistory(true),
      ]);
      if (!cancelled) setProjectsLoaded(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [sidecarOk, refreshProjects, refreshHistory]);

  useEffect(() => {
    void refreshPending();
  }, [refreshPending]);

  useEffect(() => {
    if (!searchOpen) return;
    let cancelled = false;
    void serve
      .listHistory({ all: true, limit: 200 })
      .then(({ sessions }) => {
        if (!cancelled) setSearchHistory(sessions);
      })
      .catch(() => {
        // 侧栏已有当前项目的结果可用：搜索拿不到全量就保持空，不打断输入
      });
    return () => {
      cancelled = true;
    };
  }, [searchOpen]);

  // 首次拿到项目列表时，把「当前项目」对齐到 serve 的默认工作区（GET /workspace 的 cwd）
  useEffect(() => {
    if (activeProject) return;
    const match = projects.find((p) => p.cwd === workspace?.cwd);
    if (match) setActiveProject(match.slug);
  }, [projects, workspace, activeProject, setActiveProject]);

  // 当前项目的文件夹保持展开：切项目 = 打开文件夹，会话跟着露出来
  useEffect(() => {
    if (!activeProject) return;
    setExpand((prev) =>
      prev.open.includes(activeProject) ? prev : { ...prev, open: [...prev.open, activeProject] },
    );
    void loadProjectHistory(activeProject);
  }, [activeProject, loadProjectHistory]);

  /** 换项目：只改 serve 默认（新建落点 / 历史过滤），已开的 tab 一个都不关。 */
  const switchProject = useCallback(
    async (entry: ProjectEntry) => {
      if (entry.slug === activeProjectRef.current) return;
      try {
        await serve.switchWorkspace(entry.cwd);
        setActiveProject(entry.slug);
        setWorkspace(await serve.workspace());
        await refreshProjects(true);
        await refreshHistory(true);
      } catch (e) {
        setAppError(String(e));
      }
    },
    [setActiveProject, refreshProjects, refreshHistory],
  );

  /** 「打开目录…」：登记 + 切成当前项目。 */
  const openDirectory = useCallback(
    async (path: string) => {
      const entry = await serve.registerProject(path);
      await serve.switchWorkspace(entry.cwd);
      setActiveProject(entry.slug);
      setWorkspace(await serve.workspace());
      await refreshProjects(true);
      await refreshHistory(true);
    },
    [setActiveProject, refreshProjects, refreshHistory],
  );

  /**
   * 「打开目录…」入口：桌面壳直接弹系统目录选择器；浏览器 dev 没有原生壳，
   * 退回手输路径的对话框。用户关掉选择器 = 什么都不做（不算错误）。
   */
  const openDirectoryPicker = useCallback(async () => {
    const picked = await pickDirectory();
    if (picked.kind === "cancelled") return;
    if (picked.kind === "unsupported") {
      setOpenDirOpen(true);
      return;
    }
    try {
      await openDirectory(picked.path);
    } catch (e) {
      setAppError(String(e));
    }
  }, [openDirectory]);

  /** 建会话：显式带上当前项目目录（恢复时绝不带——沙箱要跟会话走）。 */
  const createSession = useCallback(
    async (resume?: string) => {
      if (resume) return serve.create(resume);
      const cwd = projects.find((p) => p.slug === activeProjectRef.current)?.cwd;
      return serve.create(undefined, cwd ? { workspace: cwd } : undefined);
    },
    [projects],
  );

  useEffect(() => {
    void setWindowTitle(active?.title ? `SmithCode · ${active.title}` : "SmithCode");
  }, [active?.title]);

  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    (async () => {
      try {
        const { sessions } = await serve.listOnline();
        const online = sessions.find((s) => s.id === activeId);
        if (!online || cancelled) return;
        setModelEffort(activeId, online.model ?? "", online.effort ?? "");
        if (online.permission) setPermission(activeId, online.permission);
      } catch {
        // sidecar 未起时保持占位
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId, active?.busy, setModelEffort, setPermission]);

  /**
   * 「新会话」只开一个草稿窗：不打服务端、不进历史，工作区就是当前项目；
   * 等第一条消息发出时才真正 `POST /sessions`（见 handleSend）。
   */
  const newSession = useCallback(() => {
    addDraftTab();
  }, [addDraftTab]);

  /** 在指定项目下开草稿窗：不是当前项目就先切（切项目不关已有 tab），切失败则不开。 */
  const newSessionInProject = useCallback(
    async (entry: ProjectEntry) => {
      if (entry.slug !== activeProjectRef.current) {
        await switchProject(entry);
        if (entry.slug !== activeProjectRef.current) return;
      }
      newSession();
    },
    [switchProject, newSession],
  );

  const openThread = useCallback(
    async (row: ThreadRow) => {
      if (row.open) {
        useSessionStore.getState().setActive(row.id);
        return;
      }
      try {
        const { id } = await createSession(row.id);
        addTab(id, row.title);
        void refreshProjects();
        void refreshHistory(true);
      } catch (e) {
        setAppError(String(e));
      }
    },
    [createSession, addTab, refreshProjects, refreshHistory],
  );

  const openThreadById = useCallback(
    async (id: string, title: string) => {
      if (useSessionStore.getState().tabs.some((t) => t.sessionId === id)) {
        useSessionStore.getState().setActive(id);
        return;
      }
      try {
        const { id: sid } = await createSession(id);
        addTab(sid, title || "历史会话");
      } catch (e) {
        setAppError(String(e));
      }
    },
    [createSession, addTab],
  );

  const closeThread = useCallback(
    async (id: string) => {
      if (!isDraft(id)) await serve.drop(id).catch(() => {}); // 草稿没有服务端会话可卸
      closeTab(id);
    },
    [closeTab],
  );

  const doRename = useCallback(
    async (id: string, title: string) => {
      await serve.rename(id, title);
      if (useSessionStore.getState().tabs.some((t) => t.sessionId === id)) renameTab(id, title);
      setRenameTarget(null);
      void refreshProjects();
      void refreshHistory(true);
    },
    [renameTab, refreshHistory],
  );

  const doDelete = useCallback(
    async (id: string) => {
      await serve.purge(id);
      closeTab(id);
      setDeleteTarget(null);
      void refreshProjects(true);
      void refreshHistory(true);
    },
    [closeTab, refreshHistory],
  );

  /** 执行斜杠命令；返回是否成功（失败时由调用方决定要不要回滚本地回显）。 */
  const sendDispatch = useCallback(
    async (sessionId: string, text: string): Promise<boolean> => {
      try {
        const result = await serve.dispatch(sessionId, text);
        if (result.text) notice(sessionId, "info", result.text);
        if (result.select) {
          setSelectState({
            title: result.select.title,
            command: result.select.command,
            readonly: result.select.readonly,
            items: result.select.items,
          });
        }
        if (result.wizard) {
          notice(sessionId, "warning", `向导 ${result.wizard.name} 请在终端执行（桌面面板后续支持）`);
        }
        if (result.session_reset || result.session_resume) clearEvents(sessionId);
        if (result.skipped_busy) {
          notice(sessionId, "warning", "任务运行中：技能/目标未即时生效，请等待完成或先中断");
        }
        return true;
      } catch (e) {
        notice(sessionId, "error", String(e));
        return false;
      }
    },
    [notice, clearEvents],
  );

  const handleSend = useCallback(
    async (text: string) => {
      const store = useSessionStore.getState();
      let sid = store.activeId;
      // 发出即回显：服务端要等本轮真正开跑才把这条 user 消息写进历史
      // （新会话首条实测要 2~30s），先在本地垫一条，等 session.message.ended
      // 到达再按文本摘掉——否则用户看到的是"发出去半天没动静，回复来了才一起出现"。
      const pendingId = `pending-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
      if (sid) pushPendingUser(sid, { id: pendingId, text });
      if (!sid || isDraft(sid)) {
        // 草稿窗（或一个 tab 都没有）：第一条消息发出才建会话，工作区取当前项目
        try {
          const { id } = await createSession();
          if (sid) promoteTab(sid, id);
          else {
            addTab(id);
            pushPendingUser(id, { id: pendingId, text });
          }
          sid = id;
          // 会话数变了：强刷（普通刷新有 10s 节流，详情卡上的「N 个会话」会滞后）
          void refreshProjects(true);
          void refreshHistory(true);
        } catch (e) {
          if (sid) removePendingUser(sid, pendingId); // 文本会还回输入框，别留幽灵气泡
          setAppError(String(e));
          throw e;
        }
      }
      if (text.startsWith("/")) {
        if (!(await sendDispatch(sid, text))) removePendingUser(sid, pendingId);
        return;
      }
      setBusy(sid, true);
      try {
        await serve.prompt(sid, text);
      } catch (e) {
        setBusy(sid, false);
        removePendingUser(sid, pendingId);
        // 发送失败要有声音：只把文本还回输入框，用户会以为"点了没反应"
        notice(sid, "error", `发送失败：${String(e)}`);
        throw e;
      }
    },
    [
      createSession,
      addTab,
      promoteTab,
      pushPendingUser,
      removePendingUser,
      refreshProjects,
      refreshHistory,
      sendDispatch,
      setBusy,
      notice,
    ],
  );

  const toggleSidebar = useCallback(
    () => setSidebarOpen(!useSessionStore.getState().sidebarOpen),
    [setSidebarOpen],
  );

  const loadSkill = useCallback(
    (name: string) => {
      const sid = useSessionStore.getState().activeId;
      if (!sid || isDraft(sid)) return;
      setSettingsSection(null);
      void sendDispatch(sid, `/${name}`);
    },
    [sendDispatch],
  );

  const rescanSkills = useCallback(() => {
    const sid = useSessionStore.getState().activeId;
    if (sid && !isDraft(sid)) void sendDispatch(sid, "/skills refresh");
  }, [sendDispatch]);

  const closeSideWindow = useCallback((id: number) => {
    setSideWindows((wins) => {
      const next = wins.filter((w) => w.id !== id);
      setSideActive((cur) => {
        if (cur !== id) return cur;
        return next.length > 0 ? next[next.length - 1].id : null;
      });
      return next;
    });
  }, []);

  const addSideWindow = useCallback((kind: Exclude<RightPaneKind, null>) => {
    sideSeq.current += 1;
    const id = sideSeq.current;
    setSideWindows((wins) => [...wins, { id, kind }]);
    setSideActive(id);
  }, []);

  const openPane = useCallback(
    (kind: Exclude<RightPaneKind, null>) => {
      if (!useSessionStore.getState().rightOpen) setRightOpen(true);
      setSideWindows((wins) => {
        if (wins.length === 0) {
          sideSeq.current += 1;
          const id = sideSeq.current;
          queueMicrotask(() => setSideActive(id));
          return [{ id, kind }];
        }
        const activeWin = wins.find((w) => w.id === sideActiveRef.current)
          ?? wins[wins.length - 1];
        if (activeWin.kind === kind) {
          queueMicrotask(() => closeSideWindow(activeWin.id));
          return wins;
        }
        const existing = wins.find((w) => w.kind === kind);
        if (existing) {
          queueMicrotask(() => setSideActive(existing.id));
          return wins;
        }
        queueMicrotask(() => setSideActive(activeWin.id));
        return wins.map((w) => (w.id === activeWin.id ? { ...w, kind } : w));
      });
    },
    [setRightOpen, closeSideWindow],
  );

  const toggleRight = useCallback(() => {
    const isOpen = useSessionStore.getState().rightOpen;
    if (isOpen) {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
      setSideClosing(true);
      closeTimer.current = window.setTimeout(() => {
        setRightOpen(false);
        closeTimer.current = null;
      }, 220);
    } else {
      if (closeTimer.current !== null) {
        window.clearTimeout(closeTimer.current);
        closeTimer.current = null;
      }
      setSideClosing(false);
      setRightOpen(true);
    }
  }, [setRightOpen]);

  useEffect(() => {
    let unlisten: (() => void) | null = null;
    void listenMenu((cmd) => {
      switch (cmd) {
        case "new-chat":
          void newSession();
          break;
        case "close-chat": {
          const id = useSessionStore.getState().activeId;
          if (id) void closeThread(id);
          break;
        }
        case "search":
          setSearchOpen(true);
          break;
        case "toggle-sidebar":
          toggleSidebar();
          break;
        case "open-files":
          openPane("files");
          break;
        case "open-browser":
          openPane("browser");
          break;
        case "open-terminal":
          openPane("terminal");
          break;
        case "open-diff": {
          const tabs = useSessionStore.getState().tabs;
          const cur = tabs.find(
            (t) => t.sessionId === useSessionStore.getState().activeId,
          );
          if (cur && hasDiffs(cur.events)) openPane("diff");
          break;
        }
        case "open-settings":
          setSettingsSection("appearance");
          break;
        case "about":
          setSettingsSection("about");
          break;
        default:
          break;
      }
    }).then((un) => {
      unlisten = un;
    });
    return () => unlisten?.();
  }, [newSession, closeThread, toggleSidebar, toggleRight, openPane]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.ctrlKey && e.key === "Tab") {
        e.preventDefault();
        const list = useSessionStore.getState().tabs;
        const current = useSessionStore.getState().activeId;
        if (list.length < 2 || !current) return;
        const idx = list.findIndex((t) => t.sessionId === current);
        const next = list[(idx + (e.shiftKey ? -1 : 1) + list.length) % list.length];
        useSessionStore.getState().setActive(next.sessionId);
        return;
      }
      if (isTauri()) return; // 其余快捷键由原生菜单 accelerator 承担
      if (!(e.metaKey || e.ctrlKey)) return;
      const key = e.key.toLowerCase();
      if (key === "n") {
        e.preventDefault();
        void newSession();
      } else if (key === "k") {
        e.preventDefault();
        setSearchOpen(true);
      } else if (key === "b") {
        e.preventDefault();
        toggleSidebar();
      } else if (key === "p") {
        e.preventDefault();
        openPane("files");
      } else if (key === "t") {
        e.preventDefault();
        openPane("browser");
      } else if (key === "d") {
        e.preventDefault();
        const list = useSessionStore.getState().tabs;
        const cur = list.find(
          (t) => t.sessionId === useSessionStore.getState().activeId,
        );
        if (cur && hasDiffs(cur.events)) openPane("diff");
      } else if (e.key === "`") {
        e.preventDefault();
        openPane("terminal");
      } else if (e.key === ",") {
        e.preventDefault();
        setSettingsSection((s) => s ?? "appearance");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newSession, toggleSidebar, openPane]);

  if (sidecarOk === false) {
    return <BootScreen onReady={() => setSidecarOk(true)} />;
  }

  if (sidecarOk === null) {
    return (
      <div className="boot">
        <h2>SmithCode</h2>
        <p className="boot-hint">正在连接 sidecar…</p>
      </div>
    );
  }

  // 草稿窗还没有服务端会话：凡是要按会话 id 打后端的组件都拿到 null（否则 404）
  const activeSessionId = active && !isDraft(active.sessionId) ? active.sessionId : null;
  // 本地垫的 user 消息排在最后：它是最新发出的，服务端记下后会由 onEvent 摘掉
  const items = active
    ? [
        ...reduceChatItems(active.events),
        ...(active.pendingUsers ?? []).map((p) => ({
          key: p.id,
          kind: "user" as const,
          text: p.text,
        })),
      ]
    : [];

  return (
    <div className="app">
      <div className="main">
        {sidebarOpen ? (
          <Sidebar
            workspace={workspace}
            projects={projects}
            projectsLoaded={projectsLoaded}
            activeProject={activeProject}
            onSwitchProject={(p) => void switchProject(p)}
            onOpenDirectory={() => void openDirectoryPicker()}
            onRefreshProjects={() => void refreshProjects(true)}
            expand={expand}
            onToggleExpand={toggleProjectExpand}
            onToggleShowAll={toggleProjectShowAll}
            onRefreshHistory={() => void refreshHistory(true)}
            onOpenSettings={(section) => setSettingsSection(section)}
            onOpenSearch={() => setSearchOpen(true)}
            onNewSession={() => void newSession()}
            onNewSessionInProject={(p) => void newSessionInProject(p)}
            onOpenThread={(row) => void openThread(row)}
            onCloseThread={(id) => void closeThread(id)}
            onRenameThread={(id, title) => setRenameTarget({ id, title })}
            onDeleteThread={(id, title) => setDeleteTarget({ id, title })}
          />
        ) : null}

        <div className="chat">
          {!rightOpen ? <ChatTopActions onToggleRight={toggleRight} /> : null}
          {appError ? (
            <div className="app-error">
              <span className="grow">{appError}</span>
              <button className="icon" aria-label="关闭提示" onClick={() => setAppError("")}>
                <X size={13} />
              </button>
            </div>
          ) : null}
          {active && items.length > 0 ? (
            <MessageList items={items} streaming={active.busy} />
          ) : (
            <EmptyState workspaceName={workspace?.name} />
          )}
          <Composer
            sessionId={activeSessionId}
            workspace={workspace}
            model={active?.model ?? ""}
            effort={active?.effort ?? ""}
            permission={active?.permission ?? "smith"}
            busy={active?.busy ?? false}
            onSend={handleSend}
            onStop={() => {
              if (!activeSessionId) return;
              const sid = activeSessionId;
              void serve.interrupt(sid).then(
                () => {
                  // 结束事件可能因 SSE 断线收不到：按服务端状态补一次校正，
                  // 别让停止按钮在任务已经停了之后还一直亮着
                  window.setTimeout(() => void resyncBusy(), 600);
                },
                () => notice(sid, "error", "停止失败：sidecar 未响应"),
              );
            }}
            onPermissionChanged={(value) => {
              if (activeSessionId) setPermission(activeSessionId, value);
            }}
            onModelChanged={(model) => {
              if (activeSessionId) setModelEffort(activeSessionId, model, active?.effort ?? "");
            }}
            onEffortChanged={(effort) => {
              if (activeSessionId) setModelEffort(activeSessionId, active?.model ?? "", effort);
            }}
          />
        </div>

        {rightOpen ? (
          <RightSidebar
            closing={sideClosing}
            windows={sideWindows}
            activeId={sideActive}
            sessionId={activeSessionId}
            onToggle={toggleRight}
            onActivate={setSideActive}
            onClose={closeSideWindow}
            onOpen={openPane}
            onAdd={addSideWindow}
          />
        ) : null}
      </div>

      {/* 只弹当前会话的询问：后台 tab 的等待者由侧栏角标提示（见 refreshPending），
          作答端点按询问自己的 session_id 走，不跟着当前 tab 串门 */}
      {pendings
        .filter((p) => activeSessionId && p.session_id === activeSessionId)
        .map((p) =>
          p.kind === "ask_user" ? (
            <AskUserModal key={p.id} sessionId={p.session_id} pending={p} />
          ) : (
            <PermissionModal key={p.id} sessionId={p.session_id} pending={p} />
          ),
        )}

      {selectState ? (
        <SelectModal
          state={selectState}
          onClose={() => setSelectState(null)}
          onPick={(value) => {
            const command = selectState.command ? `/${selectState.command} ${value}` : `/${value}`;
            setSelectState(null);
            const sid = useSessionStore.getState().activeId;
            if (sid && !isDraft(sid)) void sendDispatch(sid, command);
          }}
        />
      ) : null}

      {searchOpen ? (
        <SearchDialog
          history={searchHistory.length > 0 ? searchHistory : history}
          onPick={(item) => void openThreadById(item.id, item.title || item.first_prompt)}
          onClose={() => setSearchOpen(false)}
        />
      ) : null}

      {renameTarget ? (
        <RenameDialog
          initial={renameTarget.title}
          onClose={() => setRenameTarget(null)}
          onSubmit={(title) => doRename(renameTarget.id, title)}
        />
      ) : null}

      {openDirOpen ? (
        <OpenDirDialog
          onClose={() => setOpenDirOpen(false)}
          onSubmit={(path) => openDirectory(path)}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmDialog
          title={`删除会话「${deleteTarget.title}」？`}
          detail="会话记录会从磁盘删除，无法恢复；正在运行的会话请先停止。"
          confirmLabel="删除"
          danger
          onClose={() => setDeleteTarget(null)}
          onConfirm={() => doDelete(deleteTarget.id)}
        />
      ) : null}

      {settingsSection ? (
        <SettingsDialog
          section={settingsSection}
          onSection={setSettingsSection}
          onClose={() => setSettingsSection(null)}
          mode={mode}
          onTheme={setMode}
          workspace={workspace}
          sessionId={activeSessionId}
          onLoadSkill={loadSkill}
          onRescanSkills={rescanSkills}
        />
      ) : null}
    </div>
  );
}
