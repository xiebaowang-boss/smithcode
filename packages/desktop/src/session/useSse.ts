import { useEffect, useRef } from "react";
import type { SmithEnvelope } from "./protocol";
import { serve } from "./serve";
import { publishSseStatus } from "./useSseStatus";

/**
 * SSE 事件流：`fetch + ReadableStream` 自读（不用原生 EventSource）。
 *
 * 原生 EventSource 发不了 `Authorization` header，只能把 token 拼进 URL——
 * 日志/历史/dump 全收。流程是：`POST /events/ticket` 换一次性票据（header 鉴权）
 * → `?ticket=` 建流 → 按 SSE 帧解析。票据 30s 有效、一次有效，偷到 URL 也没用。
 *
 * 游标按会话记（`cursors` 参数）：单游标多会话共用时，开旧会话会把所有会话
 * 全量重放一遍。重连指数退避（ref 里记 backoff，cleanup 清 timer）。
 */
export function useSse(
  sessionIds: string[],
  onEvent: (env: SmithEnvelope) => void,
  onPendingRefresh?: (sessionId?: string) => void,
  onReconnect?: () => void,
): void {
  const cursors = useRef<Map<string, number>>(new Map());
  const maxSeq = useRef(0);
  const backoffRef = useRef(1000);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const onPendingRef = useRef(onPendingRefresh);
  onPendingRef.current = onPendingRefresh;
  const onReconnectRef = useRef(onReconnect);
  onReconnectRef.current = onReconnect;
  const key = sessionIds.slice().sort().join(",");

  useEffect(() => {
    if (sessionIds.length === 0) {
      publishSseStatus({ connected: false, lastSeq: maxSeq.current });
      return;
    }
    let closed = false;
    let timer: number | undefined;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let ctrl: AbortController | null = null;

    function scheduleReconnect() {
      if (closed) return;
      publishSseStatus({ connected: false, lastSeq: maxSeq.current });
      timer = window.setTimeout(() => {
        if (closed) return;
        onPendingRef.current?.();
        // 断线期间可能错过 execution 结束事件：重连后按服务端 busy 校正 tab 状态
        onReconnectRef.current?.();
        void connect();
      }, backoffRef.current);
      backoffRef.current = Math.min(backoffRef.current * 2, 15000);
    }

    async function connect() {
      if (closed) return;
      // 关掉的会话清游标：cursors 只增不删会越积越多
      for (const id of [...cursors.current.keys()]) {
        if (!sessionIds.includes(id)) cursors.current.delete(id);
      }
      let ticket: string;
      try {
        const res = await serve.issueTicket();
        ticket = res.ticket;
      } catch {
        scheduleReconnect();
        return;
      }
      if (closed) return;
      const params = new URLSearchParams();
      for (const id of sessionIds) params.append("session_id", id);
      const cursorObj: Record<string, number> = {};
      for (const id of sessionIds) cursorObj[id] = cursors.current.get(id) ?? 0;
      params.set("cursors", JSON.stringify(cursorObj));
      params.set("limit", "500");
      params.set("ticket", ticket);
      const port = (() => {
        try {
          return localStorage.getItem("smith.port") ?? "18789";
        } catch {
          return "18789";
        }
      })();
      const url = `http://127.0.0.1:${port}/events?${params.toString()}`;
      ctrl = new AbortController();
      let resp: Response;
      try {
        resp = await fetch(url, { signal: ctrl.signal });
      } catch {
        scheduleReconnect(); // 网络错/端口未起：退避重试
        return;
      }
      if (closed) {
        void resp.body?.cancel().catch(() => {});
        return;
      }
      if (resp.status === 404) {
        // 未知会话（已 purge）：别无限重连，等上层关 tab
        publishSseStatus({ connected: false, lastSeq: maxSeq.current });
        return;
      }
      if (!resp.ok || !resp.body) {
        scheduleReconnect();
        return;
      }
      backoffRef.current = 1000;
      publishSseStatus({ connected: true, lastSeq: maxSeq.current });
      const decoder = new TextDecoder();
      reader = resp.body.getReader();
      let buf = "";
      // 无条件心跳兜底：服务端 15s 发 ping，这里 30s 没任何帧就当断了重连
      let lastFrame = Date.now();
      const watchdog = window.setInterval(() => {
        if (!closed && Date.now() - lastFrame > 30000) {
          ctrl?.abort();
        }
      }, 5000);
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (closed) break;
          if (done) break;
          lastFrame = Date.now();
          buf += decoder.decode(value, { stream: true });
          let idx: number;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            lastFrame = Date.now();
            for (const line of frame.split("\n")) {
              if (!line.startsWith("data: ")) continue;
              const payload = line.slice(6);
              let data: { type?: string } & Record<string, unknown>;
              try {
                data = JSON.parse(payload);
              } catch {
                continue; // 坏行忽略（serve 端已脱敏，重放容忍优先）
              }
              if (data.type === "ping") continue;
              if (data.type === "replay.truncated") {
                // 服务端截断续跑：把游标推到 truncated 的位置，下次重连继续拉
                const t = data.truncated as Record<string, number> | undefined;
                if (t) {
                  for (const [sid, seq] of Object.entries(t)) {
                    if (typeof seq === "number") cursors.current.set(sid, seq);
                  }
                }
                continue;
              }
              const env = data as unknown as SmithEnvelope;
              if (typeof env.seq === "number") {
                if (env.session_id) {
                  const current = cursors.current.get(env.session_id) ?? 0;
                  if (env.seq > current) cursors.current.set(env.session_id, env.seq);
                }
                if (env.seq > maxSeq.current) {
                  maxSeq.current = env.seq;
                  publishSseStatus({ connected: true, lastSeq: env.seq });
                }
              }
              if (env.type === "session.prompt.finished") {
                // 有限状态机的另一半：finished 是 durable（重放能补），本地先乐观
                // 假设弹窗会关——真正的移除由 refreshPending 的快照确认
                onPendingRef.current?.(env.session_id ?? undefined);
              } else if (env.type === "session.prompt.started") {
                // 指名会话刷：询问落在后台 tab 时，那边的角标也要立刻亮
                onPendingRef.current?.(env.session_id ?? undefined);
              }
              onEventRef.current(env);
            }
          }
        }
      } catch {
        // 读流中断（abort/断线）：退避重连
      } finally {
        window.clearInterval(watchdog);
        try {
          reader?.releaseLock();
        } catch {
          // 忽略
        }
        reader = null;
      }
      scheduleReconnect();
    }

    void connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ctrl?.abort();
      if (reader) {
        reader.cancel().catch(() => {});
        reader = null;
      }
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
}
