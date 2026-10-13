import { create } from "zustand";
import type { SmithEnvelope } from "./protocol";
import type { ProjectEntry } from "./serve";

/** 已发出、服务端还没写进历史的用户消息（本地乐观回显，见 App.handleSend）。 */
export interface PendingUser {
  id: string;
  text: string;
}

export interface TabState {
  sessionId: string;
  title: string;
  busy: boolean;
  pendingCount: number;
  events: SmithEnvelope[];
  lastSeq: number;
  model: string;
  effort: string;
  permission: string;
  pendingUsers?: PendingUser[];
}

/** 草稿 tab 的 id 前缀：本地占位，服务端还没有这个会话。 */
const DRAFT_PREFIX = "draft-";

let draftSeq = 0;

export function newDraftId(): string {
  draftSeq += 1;
  return `${DRAFT_PREFIX}${Date.now().toString(36)}-${draftSeq}`;
}

/** 该 id 是不是本地草稿（真实会话 id 是 uuid，不会带此前缀）。 */
export function isDraft(id: string | null | undefined): boolean {
  return !!id && id.startsWith(DRAFT_PREFIX);
}

interface SessionStore {
  tabs: TabState[];
  activeId: string | null;
  navStack: string[];
  navIndex: number;
  sidebarOpen: boolean;
  rightOpen: boolean;
  projects: ProjectEntry[];
  activeProject: string | null;
  addTab: (sessionId: string, title?: string) => void;
  /** 草稿窗：只开本地 tab，服务端会话等第一条消息发出才创建。返回草稿 id。 */
  addDraftTab: () => string;
  /** 草稿 → 真实会话：原地换 id（tab / 导航栈 / 活跃项一起换，内容保留）。 */
  promoteTab: (draftId: string, realId: string) => void;
  closeTab: (sessionId: string) => void;
  setActive: (sessionId: string) => void;
  goBack: () => void;
  goForward: () => void;
  setSidebarOpen: (open: boolean) => void;
  setRightOpen: (open: boolean) => void;
  renameTab: (sessionId: string, title: string) => void;
  pushEvent: (sessionId: string, env: SmithEnvelope) => void;
  setBusy: (sessionId: string, busy: boolean) => void;
  setPendingCount: (sessionId: string, n: number) => void;
  setModelEffort: (sessionId: string, model: string, effort: string) => void;
  setPermission: (sessionId: string, mode: string) => void;
  clearEvents: (sessionId: string) => void;
  pushPendingUser: (sessionId: string, item: PendingUser) => void;
  removePendingUser: (sessionId: string, id: string) => void;
  /** 服务端把这条 user 消息记下了：摘掉对应的本地回显，避免两条气泡。 */
  resolvePendingUser: (sessionId: string, text: string) => void;
  setProjects: (projects: ProjectEntry[]) => void;
  setActiveProject: (slug: string | null) => void;
}

const MAX_EVENTS_PER_TAB = 2000;

/// 导航栈上限：无界 push 会让长会话越积越多
const MAX_NAV_STACK = 100;

function pushNav(stack: string[], index: number, sessionId: string): { navStack: string[]; navIndex: number } {
  const base = stack.slice(0, index + 1);
  // 连续重复激活不入栈（如重复点同一会话）
  if (base[base.length - 1] === sessionId) {
    return { navStack: stack, navIndex: index };
  }
  const navStack = [...base, sessionId];
  const trimmed =
    navStack.length > MAX_NAV_STACK
      ? navStack.slice(navStack.length - MAX_NAV_STACK)
      : navStack;
  return { navStack: trimmed, navIndex: trimmed.length - 1 };
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  tabs: [],
  activeId: null,
  navStack: [],
  navIndex: -1,
  sidebarOpen: true,
  rightOpen: false,
  projects: [],
  activeProject: null,
  addTab: (sessionId, title = "新会话") =>
    set((s) => {
      const tabs = s.tabs.some((t) => t.sessionId === sessionId)
        ? s.tabs
        : [
            ...s.tabs,
            {
              sessionId,
              title,
              busy: false,
              pendingCount: 0,
              events: [],
              lastSeq: 0,
              model: "",
              effort: "",
              permission: "smith",
            },
          ];
      const { navStack, navIndex } = pushNav(s.navStack, s.navIndex, sessionId);
      return { tabs, activeId: sessionId, navStack, navIndex };
    }),
  addDraftTab: () => {
    // 已经有个草稿窗就直接切过去：草稿是空窗口，重复开只会攒一堆空 tab
    const existing = get().tabs.find((t) => isDraft(t.sessionId));
    if (existing) {
      get().setActive(existing.sessionId);
      return existing.sessionId;
    }
    const id = newDraftId();
    get().addTab(id);
    return id;
  },
  promoteTab: (draftId, realId) =>
    set((s) => {
      if (!s.tabs.some((t) => t.sessionId === draftId)) return {};
      if (s.tabs.some((t) => t.sessionId === realId)) {
        // 极端情况（同一真实 id 已开 tab）：丢草稿、切到已有的，别开出两个
        return {
          tabs: s.tabs.filter((t) => t.sessionId !== draftId),
          activeId: realId,
          navStack: s.navStack.filter((id) => id !== draftId),
          navIndex: Math.max(0, s.navStack.lastIndexOf(realId)),
        };
      }
      const tabs = s.tabs.map((t) =>
        t.sessionId === draftId ? { ...t, sessionId: realId } : t,
      );
      // 导航栈按位置换 id；换完去掉因此产生的相邻重复
      const navStack = s.navStack.reduce<string[]>((acc, id) => {
        const next = id === draftId ? realId : id;
        if (acc[acc.length - 1] !== next) acc.push(next);
        return acc;
      }, []);
      const activeId = s.activeId === draftId ? realId : s.activeId;
      const navIndex = activeId ? Math.max(0, navStack.lastIndexOf(activeId)) : s.navIndex;
      return { tabs, navStack, navIndex, activeId };
    }),
  closeTab: (sessionId) =>
    set((s) => {
      const tabs = s.tabs.filter((t) => t.sessionId !== sessionId);
      const activeId =
        s.activeId === sessionId ? (tabs[tabs.length - 1]?.sessionId ?? null) : s.activeId;
      const navStack = s.navStack.filter((id) => id !== sessionId);
      // 关中间 tab 后按新 activeId 定位，而不是 min(old, len-1)
      const navIndex = activeId ? Math.max(0, navStack.lastIndexOf(activeId)) : -1;
      return {
        tabs,
        activeId,
        navStack,
        navIndex: navStack.length === 0 ? -1 : navIndex,
      };
    }),
  setActive: (sessionId) =>
    set((s) => {
      if (s.activeId === sessionId) return {};
      const { navStack, navIndex } = pushNav(s.navStack, s.navIndex, sessionId);
      return { activeId: sessionId, navStack, navIndex };
    }),
  goBack: () =>
    set((s) => {
      let idx = s.navIndex - 1;
      while (idx >= 0 && !s.tabs.some((t) => t.sessionId === s.navStack[idx])) idx -= 1;
      if (idx < 0) return {};
      return { navIndex: idx, activeId: s.navStack[idx] };
    }),
  goForward: () =>
    set((s) => {
      let idx = s.navIndex + 1;
      while (idx < s.navStack.length && !s.tabs.some((t) => t.sessionId === s.navStack[idx])) {
        idx += 1;
      }
      if (idx >= s.navStack.length) return {};
      return { navIndex: idx, activeId: s.navStack[idx] };
    }),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setRightOpen: (open) => set({ rightOpen: open }),
  setProjects: (projects) => set({ projects }),
  setActiveProject: (slug) => set({ activeProject: slug }),
  renameTab: (sessionId, title) =>
    set((s) => ({
      tabs: s.tabs.map((t) => (t.sessionId === sessionId ? { ...t, title } : t)),
    })),
  pushEvent: (sessionId, env) =>
    set((s) => {
      const tab = s.tabs.find((t) => t.sessionId === sessionId);
      if (!tab) return {};
      // 两条去重线：重放的事件带 seq（游标水位挡掉更旧的）；直播的事件不带 seq
      // （seq 是日志写盘时才分配的），靠事件 id 与重放副本认出同一条。缺了后者，
      // 一次重连就会把整段历史再渲染一遍；少了前者，断线期间压着的旧事件会重排。
      if (typeof env.seq === "number" && env.seq <= tab.lastSeq) return {};
      // id 去重只看尾部（新到的总在尾部）：O(2000) 全扫在流式 burst 下是 O(n²)
      let dup = false;
      for (let i = tab.events.length - 1; i >= Math.max(0, tab.events.length - 64); i--) {
        if (tab.events[i].id === env.id) {
          dup = true;
          break;
        }
      }
      if (dup) return {};
      // 旧事件也可能重放回来（如 after_seq 回退）：全量扫一遍确认
      if (tab.events.length > 64 && tab.events.some((e) => e.id === env.id)) return {};
      const events = [...tab.events, env];
      const trimmed =
        events.length > MAX_EVENTS_PER_TAB
          ? events.slice(events.length - MAX_EVENTS_PER_TAB)
          : events;
      const next = {
        ...tab,
        events: trimmed,
        lastSeq: typeof env.seq === "number" && env.seq > tab.lastSeq ? env.seq : tab.lastSeq,
      };
      return {
        tabs: s.tabs.map((t) => (t.sessionId === sessionId ? next : t)),
      };
    }),
  setBusy: (sessionId, busy) =>
    set((s) => ({
      tabs: s.tabs.map((t) => (t.sessionId === sessionId ? { ...t, busy } : t)),
    })),
  setPendingCount: (sessionId, n) =>
    set((s) => ({
      tabs: s.tabs.map((t) => (t.sessionId === sessionId ? { ...t, pendingCount: n } : t)),
    })),
  setModelEffort: (sessionId, model, effort) =>
    set((s) => ({
      tabs: s.tabs.map((t) =>
        t.sessionId === sessionId && (t.model !== model || t.effort !== effort)
          ? { ...t, model, effort }
          : t,
      ),
    })),
  setPermission: (sessionId, mode) =>
    set((s) => ({
      tabs: s.tabs.map((t) => (t.sessionId === sessionId ? { ...t, permission: mode } : t)),
    })),
  clearEvents: (sessionId) =>
    // 只清可见事件、保留 seq 水位：/new 重置后若 SSE 重连，重放的旧记录不再回到列表。
    // 同时清掉上一轮的 busy/pending：reset 后 spinner + 弹窗不能留给新会话。
    set((s) => ({
      tabs: s.tabs.map((t) =>
        t.sessionId === sessionId
          ? { ...t, events: [], busy: false, pendingCount: 0, pendingUsers: [] }
          : t,
      ),
    })),
  pushPendingUser: (sessionId, item) =>
    set((s) => ({
      tabs: s.tabs.map((t) =>
        t.sessionId === sessionId
          ? { ...t, pendingUsers: [...(t.pendingUsers ?? []), item] }
          : t,
      ),
    })),
  removePendingUser: (sessionId, id) =>
    set((s) => ({
      tabs: s.tabs.map((t) =>
        t.sessionId === sessionId && t.pendingUsers
          ? { ...t, pendingUsers: t.pendingUsers.filter((p) => p.id !== id) }
          : t,
      ),
    })),
  resolvePendingUser: (sessionId, text) =>
    set((s) => ({
      tabs: s.tabs.map((t) => {
        if (t.sessionId !== sessionId || !t.pendingUsers?.length) return t;
        // 按文本对账（先进先出）：对不上说明这条不是本地垫的（历史回放/系统回写），别动
        const idx = t.pendingUsers.findIndex((p) => p.text === text);
        if (idx < 0) return t;
        const pendingUsers = t.pendingUsers.slice();
        pendingUsers.splice(idx, 1);
        return { ...t, pendingUsers };
      }),
    })),
}));
