import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 全局错误边界：一条坏 envelope、一个畸形 tool 结果，都不该让整个桌面端白屏。
 *
 * 覆盖范围是整个 App（含消息区/弹窗/侧栏）——React 的错误边界只能捕渲染期
 * 异常（事件处理器与异步里的错误仍要走各自的 try/catch），但"收到畸形事件
 * 后渲染崩掉"正是这里最常见的故障形态。
 *
 * 恢复策略：先给一次"重试"（清掉 error 状态重渲染）；仍崩就提示刷新。
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; onReset?: () => void },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 保留到控制台便于排查（生产构建里这也进 DevTools，不进用户界面）
    console.error("[SmithCode] 渲染异常", error, info.componentStack);
  }

  private reset = () => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="boot">
        <h2>SmithCode 遇到一个界面错误</h2>
        <p className="boot-hint">已捕获渲染异常，会话数据不受影响。</p>
        <pre className="error-boundary-detail">{String(this.state.error.message || this.state.error)}</pre>
        <div className="modal-options">
          <button className="primary" onClick={this.reset}>
            重试
          </button>
          <button onClick={() => window.location.reload()}>刷新应用</button>
        </div>
      </div>
    );
  }
}
