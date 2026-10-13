import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowLeft,
  ArrowRight,
  Blocks,
  ChevronRight,
  Copy,
  Ellipsis,
  Folder,
  Loader2,
  MessageSquare,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  SquarePen,
  Trash2,
  X,
} from "lucide-react";
import { isDraft, useSessionStore, type TabState } from "../session/sessionStore";
import type { HistorySession, ProjectEntry, WorkspaceInfo } from "../session/serve";
import type { SettingsSection } from "../settings/sections";
import { Menu, type MenuItem } from "../ui/Menu";
import { relTime } from "../ui/format";
import pkg from "../../package.json";

export interface ThreadRow {
  id: string;
  title: string;
  busy: boolean;
  pending: number;
  open: boolean;
  updated: number;
  tooltip: string;
}

/** 单个项目文件夹的展开态：展开哪些项目、各自拉到的会话、加载与「更多」状态 */
export interface ProjectExpand {
  /** 已展开的项目 slug */
  open: string[];
  /** slug -> 该项目的会话（由 App 按需拉取） */
  rows: Record<string, HistorySession[]>;
  /** 正在拉取的项目 slug 列表（可并发） */
  loading: string[];
  /** 最近一次拉取失败的提示 */
  error: string;
  /** 已点「还有 N 个」展开全部的项目 slug */
  showAll: string[];
}

function mergeThreads(tabs: TabState[], history: HistorySession[]): ThreadRow[] {
  // tabs 是全局的（所有已开会话），这里只留「确实属于本项目」的那些：
  // 按本项目的会话清单过滤，否则每个项目文件夹里都会重复列出别的项目的已开会话
  const known = new Set(history.map((h) => h.id));
  const mine = tabs.filter((t) => known.has(t.sessionId));
  const openIds = new Set(mine.map((t) => t.sessionId));
  const rows: ThreadRow[] = mine.map((t) => {
    const h = history.find((item) => item.id === t.sessionId);
    return {
      id: t.sessionId,
      title: t.title,
      busy: t.busy,
      pending: t.pendingCount,
      open: true,
      updated: Number.MAX_SAFE_INTEGER - tabs.indexOf(t),
      tooltip: [h?.model, h ? relTime(h.updated) : ""].filter(Boolean).join(" · "),
    };
  });
  for (const h of history) {
    if (openIds.has(h.id)) continue;
    rows.push({
      id: h.id,
      title: h.title || h.first_prompt || h.id.slice(0, 8),
      busy: false,
      pending: 0,
      open: false,
      updated: h.updated,
      tooltip: [h.model, relTime(h.updated), h.first_prompt].filter(Boolean).join(" · "),
    });
  }
  rows.sort((a, b) => b.updated - a.updated);
  return rows;
}

function ThreadItem({
  row,
  nested,
  active,
  onOpen,
  onRename,
  onDelete,
  onClose,
}: {
  row: ThreadRow;
  nested?: boolean;
  active: boolean;
  onOpen: () => void;
  onRename: () => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const items: MenuItem[] = [
    { label: "重命名", icon: <Pencil size={13} />, onSelect: onRename },
    ...(row.open
      ? [{ label: "关闭（保留记录）", icon: <X size={13} />, onSelect: onClose }]
      : []),
    { label: "删除（不可恢复）", icon: <Trash2 size={13} />, danger: true, onSelect: onDelete },
  ];
  return (
    <div
      className={`sidebar-item${nested ? " sidebar-thread" : ""}${active ? " active" : ""}`}
      role="button"
      tabIndex={0}
      title={row.tooltip}
      aria-current={active}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      {row.busy ? <Loader2 size={12} className="spin" /> : null}
      {row.pending > 0 ? <span className="badge">{row.pending}</span> : null}
      <span className="grow sidebar-item-title">{row.title}</span>
      <span className={`sidebar-item-actions${menuOpen ? " open" : ""}`}>
        <span className="popover-anchor">
          <button
            aria-label={`${row.title} 的更多操作`}
            onClick={(e) => {
              e.stopPropagation();
              setMenuOpen(!menuOpen);
            }}
          >
            <Ellipsis size={14} />
          </button>
          {menuOpen ? (
            <Menu alignRight items={items} onClose={() => setMenuOpen(false)} />
          ) : null}
        </span>
      </span>
    </div>
  );
}

/** 项目详情浮层的位置：按行的视口坐标算，portal 到 body（侧栏滚动区会被裁剪） */
interface CardState {
  entry: ProjectEntry;
  top: number;
  left: number;
}

/**
 * 项目 hover 出的详情卡：名称、会话数、路径（点击复制）、新建会话。
 * 鼠标可以移进卡片本身（离开行不立即消失，由 Sidebar 延时兜住）。
 */
function ProjectCard({
  card,
  onEnter,
  onLeave,
  onNewSession,
}: {
  card: CardState;
  onEnter: () => void;
  onLeave: () => void;
  onNewSession: () => void;
}) {
  const p = card.entry;
  const [copied, setCopied] = useState(false);
  const copyPath = () => {
    void navigator.clipboard.writeText(p.cwd);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div
      className="popover project-card"
      role="dialog"
      aria-label={`${p.name} 项目详情`}
      style={{ top: card.top, left: card.left }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <div className="pc-head">
        <Folder size={14} />
        <span className="grow sidebar-item-title">{p.name}</span>
      </div>
      <div className="pc-row">
        <MessageSquare size={13} />
        <span>{p.sessionCount} 个会话</span>
      </div>
      <div className="pc-sep" />
      <button className="pc-row" title="点击复制项目路径" onClick={copyPath}>
        <Folder size={13} />
        <span className="grow pc-path">{p.cwd}</span>
        <span className="pc-hint">{copied ? "已复制" : ""}</span>
      </button>
      <div className="pc-sep" />
      <button className="pc-row" onClick={onNewSession}>
        <SquarePen size={13} />
        <span>新建会话</span>
      </button>
    </div>
  );
}

export function Sidebar({
  workspace,
  projects,
  projectsLoaded,
  activeProject,
  onSwitchProject,
  onOpenDirectory,
  onRefreshProjects,
  expand,
  onToggleExpand,
  onToggleShowAll,
  onRefreshHistory,
  onOpenSettings,
  onOpenSearch,
  onNewSession,
  onNewSessionInProject,
  onOpenThread,
  onCloseThread,
  onRenameThread,
  onDeleteThread,
}: {
  workspace: WorkspaceInfo | null;
  projects: ProjectEntry[];
  /** 首屏项目列表是否已尝试拉取：未拉取时不能显示「没有项目」的占位 */
  projectsLoaded: boolean;
  activeProject: string | null;
  onSwitchProject: (project: ProjectEntry) => void;
  onOpenDirectory: () => void;
  onRefreshProjects: () => void;
  expand: ProjectExpand;
  onToggleExpand: (slug: string) => void;
  onToggleShowAll: (slug: string) => void;
  onRefreshHistory: () => void;
  onOpenSettings: (section: SettingsSection) => void;
  onOpenSearch: () => void;
  onNewSession: () => void;
  onNewSessionInProject: (project: ProjectEntry) => void;
  onOpenThread: (row: ThreadRow) => void;
  onCloseThread: (id: string) => void;
  onRenameThread: (id: string, title: string) => void;
  onDeleteThread: (id: string, title: string) => void;
}) {
  const tabs = useSessionStore((s) => s.tabs);
  const activeId = useSessionStore((s) => s.activeId);
  const navStack = useSessionStore((s) => s.navStack);
  const navIndex = useSessionStore((s) => s.navIndex);
  const goBack = useSessionStore((s) => s.goBack);
  const goForward = useSessionStore((s) => s.goForward);
  const [menuSlug, setMenuSlug] = useState<string | null>(null);
  /** 项目 hover 详情卡：项目 + 出现时算好的视口坐标 */
  const [card, setCard] = useState<CardState | null>(null);
  const hideTimer = useRef<number | null>(null);
  const cancelHide = () => {
    if (hideTimer.current !== null) {
      window.clearTimeout(hideTimer.current);
      hideTimer.current = null;
    }
  };
  /** 行进入：弹出详情卡（坐标按行的当前位置算，卡片 portal 到 body） */
  const showCard = (entry: ProjectEntry, el: HTMLElement) => {
    cancelHide();
    const r = el.getBoundingClientRect();
    const top = Math.max(8, Math.min(r.top, window.innerHeight - 230));
    setCard({ entry, top, left: r.right + 6 });
  };
  /** 行离开：延时隐藏，留出鼠标从行移进卡片的时间 */
  const scheduleHideCard = () => {
    cancelHide();
    hideTimer.current = window.setTimeout(() => {
      hideTimer.current = null;
      setCard(null);
    }, 160);
  };

  // 卡片开着时滚动/缩放就地隐藏：坐标是进入那一刻按视口算的，动了就不准
  useEffect(() => {
    if (!card) return;
    const hide = () => setCard(null);
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      document.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [card]);
  // 卸载兜底：别把延时定时器留在外面
  useEffect(() => () => cancelHide(), []);

  const backTarget = useMemo(() => {
    for (let i = navIndex - 1; i >= 0; i -= 1) {
      if (tabs.some((t) => t.sessionId === navStack[i])) return navStack[i];
    }
    return null;
  }, [navStack, navIndex, tabs]);
  const forwardTarget = useMemo(() => {
    for (let i = navIndex + 1; i < navStack.length; i += 1) {
      if (tabs.some((t) => t.sessionId === navStack[i])) return navStack[i];
    }
    return null;
  }, [navStack, navIndex, tabs]);

  const active = tabs.find((t) => t.sessionId === activeId);
  // 草稿窗还没创建会话，不该被算进「N 个会话」
  const realTabCount = tabs.filter((t) => !isDraft(t.sessionId)).length;
  const activeEntry = projects.find((p) => p.slug === activeProject) ?? null;
  const workspaceName = activeEntry?.name ?? workspace?.name ?? "工作区";

  return (
    <aside className="sidebar" aria-label="项目与会话">
      <div className="sidebar-nav">
        <button
          className="icon"
          title="后退"
          disabled={!backTarget}
          onClick={() => goBack()}
        >
          <ArrowLeft size={16} />
        </button>
        <button
          className="icon"
          title="前进"
          disabled={!forwardTarget}
          onClick={() => goForward()}
        >
          <ArrowRight size={16} />
        </button>
        {/* 顶部不再放「当前项目」：谁是当前项目看高亮行；路径/刷新等挪到项目行 hover */}
        <span className="spacer" />
        <button className="icon" title="搜索会话（Ctrl+K）" onClick={onOpenSearch}>
          <Search size={16} />
        </button>
      </div>

      <div className="sidebar-scroll">
        <button className="sidebar-item" onClick={onNewSession}>
          <SquarePen size={16} />
          <span className="grow">新会话</span>
        </button>
        <button className="sidebar-item" onClick={() => onOpenSettings("mcp")}>
          <Plug size={16} />
          <span className="grow">插件</span>
        </button>
        <button className="sidebar-item" onClick={() => onOpenSettings("skills")}>
          <Blocks size={16} />
          <span className="grow">探索</span>
        </button>

        <div className="sidebar-section">
          项目
          <span className="spacer" />
          <button
            onClick={onOpenDirectory}
            aria-label="打开目录…"
            title="登记并切换到另一个项目目录"
          >
            <span className="icon-plus" aria-hidden="true">
              <Folder size={11} />
              <span className="chip">
                <Plus size={7} strokeWidth={3} />
              </span>
            </span>
          </button>
          <button onClick={onRefreshProjects} aria-label="刷新项目列表">
            <RefreshCw size={11} />
          </button>
        </div>
        {projects.length > 0 ? (
          projects.map((p) => {
            const open = expand.open.includes(p.slug);
            const loading = expand.loading.includes(p.slug);
            const showAll = expand.showAll.includes(p.slug);
            const rows = mergeThreads(tabs, expand.rows[p.slug] ?? []);
            const visible = showAll ? rows : rows.slice(0, 6);
            return (
              <div key={p.slug} className="project-node">
                <div
                  className={`sidebar-item project-row${
                    p.slug === activeProject ? " active" : ""
                  }${open ? " open" : ""}`}
                  title={p.cwd}
                  onMouseEnter={(e) => showCard(p, e.currentTarget)}
                  onMouseLeave={scheduleHideCard}
                >
                  <button
                    className="project-caret"
                    aria-expanded={open}
                    aria-label={`${open ? "收起" : "展开"} ${p.name}`}
                    onClick={() => onToggleExpand(p.slug)}
                  >
                    <ChevronRight size={12} className="caret-icon" />
                  </button>
                  <button
                    className="project-main"
                    onClick={() => {
                      // 已是当前项目却收起了：就地展开；否则切项目（切完由 App 自动展开）
                      if (p.slug === activeProject && !open) onToggleExpand(p.slug);
                      else onSwitchProject(p);
                    }}
                  >
                    <Folder size={15} />
                    <span className="grow sidebar-item-title">{p.name}</span>
                  </button>
                  {/* hover 才露出的行内操作：更多（原顶部工作区菜单的条目）+ 在该项目下新建会话 */}
                  <span className={`sidebar-item-actions${menuSlug === p.slug ? " open" : ""}`}>
                    <span className="popover-anchor">
                      <button
                        aria-label={`${p.name} 的更多操作`}
                        onClick={(e) => {
                          e.stopPropagation();
                          const opening = menuSlug !== p.slug;
                          setMenuSlug(opening ? p.slug : null);
                          // 菜单和详情卡都是「浮层」，同时开会打架：开菜单就把卡收掉
                          if (opening) setCard(null);
                        }}
                      >
                        <Ellipsis size={14} />
                      </button>
                      {menuSlug === p.slug ? (
                        <Menu
                          alignRight
                          onClose={() => setMenuSlug(null)}
                          items={[
                            {
                              label: "复制项目路径",
                              icon: <Copy size={13} />,
                              onSelect: () => void navigator.clipboard.writeText(p.cwd),
                            },
                            {
                              label: "刷新会话列表",
                              icon: <RefreshCw size={13} />,
                              onSelect: onRefreshHistory,
                            },
                            {
                              label: "在设置中查看",
                              icon: <Settings2 size={13} />,
                              onSelect: () => onOpenSettings("about"),
                            },
                          ]}
                        />
                      ) : null}
                    </span>
                    <button
                      aria-label={`在 ${p.name} 中新建会话`}
                      title="新建会话"
                      onClick={(e) => {
                        e.stopPropagation();
                        setMenuSlug(null);
                        setCard(null);
                        onNewSessionInProject(p);
                      }}
                    >
                      <SquarePen size={14} />
                    </button>
                  </span>
                </div>
                {open ? (
                  <div className="project-children">
                    {loading && rows.length === 0 ? (
                      <div className="pane-empty">加载中…</div>
                    ) : null}
                    {visible.map((row) => (
                      <ThreadItem
                        key={row.id}
                        row={row}
                        nested
                        active={row.id === activeId}
                        onOpen={() => onOpenThread(row)}
                        onRename={() => onRenameThread(row.id, row.title)}
                        onDelete={() => onDeleteThread(row.id, row.title)}
                        onClose={() => onCloseThread(row.id)}
                      />
                    ))}
                    {!loading && rows.length === 0 ? (
                      <div className="pane-empty">还没有会话</div>
                    ) : null}
                    {rows.length > 6 ? (
                      <button className="sidebar-more" onClick={() => onToggleShowAll(p.slug)}>
                        {showAll ? "收起" : `还有 ${rows.length - 6} 个会话`}
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </div>
            );
          })
        ) : projectsLoaded ? (
          <div className="sidebar-item active">
            <Folder size={15} />
            <span className="grow sidebar-item-title">{workspaceName}</span>
          </div>
        ) : (
          <div className="pane-empty sidebar-loading" aria-busy="true">
            <Loader2 size={13} className="spin" /> 加载中…
          </div>
        )}
        {expand.error ? <div className="sidebar-error">{expand.error}</div> : null}
      </div>

      <div className="sidebar-footer">
        <button className="account-card" onClick={() => onOpenSettings("provider")}>
          <span className="grow">
            <span className="provider">{active?.model || "SmithCode"}</span>
            <span className="meta">
              v{pkg.version} · {active?.effort ? `${active.effort} · ` : ""}
              {realTabCount > 0 ? `${realTabCount} 个会话` : "未开始"}
            </span>
          </span>
        </button>
        <button className="sidebar-item" onClick={() => onOpenSettings("appearance")}>
          <Settings2 size={16} />
          <span className="grow">设置</span>
          <span className="shortcut-hint">Ctrl+,</span>
        </button>
      </div>

      {/* 详情卡挂到 body：侧栏滚动区是 overflow，绝对定位的卡片会被裁掉 */}
      {card
        ? createPortal(
            <ProjectCard
              card={card}
              onEnter={cancelHide}
              onLeave={() => setCard(null)}
              onNewSession={() => {
                setCard(null);
                onNewSessionInProject(card.entry);
              }}
            />,
            document.body,
          )
        : null}
    </aside>
  );
}
