import { isTauri } from "../session/serve";

export { isTauri };

export async function toggleFullscreen(): Promise<void> {
  if (isTauri()) {
    try {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      const win = getCurrentWindow();
      await win.setFullscreen(!(await win.isFullscreen()));
    } catch {
      // 权限或平台不支持时忽略
    }
    return;
  }
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    // 浏览器拒绝时忽略
  }
}

export async function setWindowTitle(title: string): Promise<void> {
  if (!isTauri()) {
    document.title = title;
    return;
  }
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().setTitle(title);
  } catch {
    // 忽略
  }
}

/** 目录选择结果：picked=选好了；cancelled=用户关掉了对话框；unsupported=非 Tauri 或插件不可用。 */
export type DirPick =
  | { kind: "picked"; path: string }
  | { kind: "cancelled" }
  | { kind: "unsupported" };

/** 弹系统目录选择器。浏览器 dev 下没有原生壳，返回 unsupported 让调用方退回手输路径。 */
export async function pickDirectory(): Promise<DirPick> {
  if (!isTauri()) return { kind: "unsupported" };
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const picked = await open({ directory: true, multiple: false, title: "选择项目目录" });
    if (picked === null || picked === undefined) return { kind: "cancelled" };
    return typeof picked === "string" ? { kind: "picked", path: picked } : { kind: "cancelled" };
  } catch {
    return { kind: "unsupported" };
  }
}

export type MenuCommand =
  | "new-chat"
  | "close-chat"
  | "search"
  | "toggle-sidebar"
  | "open-files"
  | "open-browser"
  | "open-terminal"
  | "open-diff"
  | "open-settings"
  | "about";

/** 原生菜单事件桥：非 Tauri 环境返回空退订函数（dev 下快捷键由 JS 兜底）。 */
export async function listenMenu(handler: (cmd: MenuCommand) => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  try {
    const { listen } = await import("@tauri-apps/api/event");
    return await listen<string>("smith://menu", (event) => {
      handler(event.payload as MenuCommand);
    });
  } catch {
    return () => {};
  }
}
