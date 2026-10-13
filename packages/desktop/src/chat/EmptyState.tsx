import { Logo } from "../app/Logo";

export function EmptyState({ workspaceName }: { workspaceName?: string }) {
  return (
    <div className="empty-state">
      <Logo size={44} />
      <h1>
        {workspaceName ? (
          <>
            在 <span className="project">{workspaceName}</span> 里做点什么？
          </>
        ) : (
          "做点什么？"
        )}
      </h1>
    </div>
  );
}
