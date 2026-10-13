import { Brain } from "lucide-react";
import { Markdown } from "./Markdown";

export function ThinkingBlock({ text }: { text: string }) {
  return (
    <details className="msg thinking">
      <summary>
        <Brain size={13} /> 思考过程（{text.length} 字，点击展开）
      </summary>
      <Markdown text={text} />
    </details>
  );
}
