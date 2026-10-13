import { useEffect, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

/**
 * 链接/图片地址白名单：只放行 http(s) / mailto 与站内相对路径。
 *
 * react-markdown 自带的 `defaultUrlTransform` 已挡 `javascript:`，但这里是
 * **防御纵深**——工具输出、MCP 错误、notice 文本都是不可信内容，一旦哪次
 * 上游放宽了默认值，显式白名单能兜住。`data:` 也排除（可用于钓鱼/内联脚本）。
 */
function safeUrl(url: string): string {
  const trimmed = url.trim();
  if (/^(https?:|mailto:|#|\/)/i.test(trimmed)) return trimmed;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return trimmed; // 相对路径（无 scheme）
  return "";
}

function CodeBlock({ language, code }: { language: string; code: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <div className="codeblock">
      <div className="codeblock-head">
        <span>{language || "text"}</span>
        <button
          onClick={() => {
            void navigator.clipboard.writeText(code).then(() => setCopied(true));
          }}
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>
        <code className={language ? `language-${language}` : undefined}>{code}</code>
      </pre>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        skipHtml
        urlTransform={safeUrl}
        components={{
          pre({ children }) {
            return <>{children}</>;
          },
          code({ className, children, ...props }) {
            const raw = String(children ?? "");
            const inPre = raw.includes("\n");
            if (!inPre) {
              return (
                <code className={className} {...props}>
                  {children as ReactNode}
                </code>
              );
            }
            const match = /language-(\w+)/.exec(className ?? "");
            return <CodeBlock language={match?.[1] ?? ""} code={raw.replace(/\n$/, "")} />;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
