import { useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { serve } from "../session/serve";

export function BootScreen({ onReady }: { onReady: () => void }) {
  const [port, setPort] = useState("18789");
  const [token, setToken] = useState("");
  const [phase, setPhase] = useState<"port" | "auth">("port");

  useEffect(() => {
    try {
      setPort(localStorage.getItem("smith.port") ?? "18789");
      setToken(localStorage.getItem("smith.token") ?? "");
    } catch {
      // 忽略
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await serve.ensureEndpoint();
      } catch {
        // 非 Tauri 环境：直接用 localStorage
      }
      for (let i = 0; i < 40 && !cancelled; i++) {
        // 壳在后台线程拉起 serve，token 要等 stdout 的 token= 行；每轮重取一次
        try {
          await serve.refreshEndpoint();
        } catch {
          // 非 Tauri 环境：直接用 localStorage
        }
        try {
          await serve.health();
          if (!cancelled) setPhase("auth");
          // /health 无鉴权：再拿一个需鉴权的端点确认 token 生效，避免主界面 401 黑洞
          try {
            await serve.listOnline();
            if (!cancelled) onReady();
            return;
          } catch {
            // 401 → token 还没同步下来（Rust 侧正在解析 stdout），继续等
          }
        } catch {
          // 端口还没起来，继续等
        }
        await new Promise((r) => setTimeout(r, 500));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [onReady]);

  return (
    <div className="boot">
      <h2>SmithCode</h2>
      {phase === "port" ? (
        <p>
          <Loader2 size={14} /> 正在等待 sidecar（smith --serve，127.0.0.1:{port}）…
        </p>
      ) : (
        <p className="boot-hint">
          <Loader2 size={14} /> sidecar 已起，等待 token 同步…
        </p>
      )}
      <p className="boot-hint">
        sidecar 未起时：终端跑 <code>smith --serve</code>（首次打印 <code>token=…</code> 填入
        localStorage 的 <code>smith.token</code>），或检查 Tauri 壳日志。
      </p>
      <button onClick={() => window.location.reload()}>
        <RefreshCw size={13} /> 重试
      </button>
      {token ? null : <p className="boot-hint">当前未配置 token（直连默认端口时可为空）。</p>}
    </div>
  );
}
