import { useEffect, useMemo, useRef, useState } from "react";
import { evaluationEventUrl } from "../api";
import type { ConnectionState, EvaluationEvent } from "../types";

export interface EventSourceLike {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  close(): void;
}

type EventSourceFactory = (url: string) => EventSourceLike;

const browserEventSource: EventSourceFactory = (url) => new EventSource(url);

export interface EvaluationEventStreamState {
  connection: ConnectionState;
  events: EvaluationEvent[];
  lastEventId?: string;
  error?: Error;
}

export function useEvaluationEventStream(
  taskId: string | undefined,
  factory: EventSourceFactory = browserEventSource,
): EvaluationEventStreamState {
  const [connection, setConnection] = useState<ConnectionState>(taskId ? "CONNECTING" : "CLOSED");
  const [events, setEvents] = useState<EvaluationEvent[]>([]);
  const [lastEventId, setLastEventId] = useState<string>();
  const [error, setError] = useState<Error>();
  const sequences = useRef(new Set<number>());

  useEffect(() => {
    if (!taskId) {
      queueMicrotask(() => setConnection("CLOSED"));
      return;
    }
    sequences.current = new Set();

    const source = factory(evaluationEventUrl(taskId));
    source.onopen = () => {
      sequences.current = new Set();
      setEvents([]);
      setLastEventId(undefined);
      setConnection("OPEN");
      setError(undefined);
    };
    source.onerror = () => {
      setConnection("RECONNECTING");
      setError(new Error("评测事件流连接中断，浏览器正在使用 Last-Event-ID 自动重连"));
    };
    const onEvent: EventListener = (raw) => {
      const message = raw as MessageEvent<string>;
      try {
        const parsed = JSON.parse(message.data) as EvaluationEvent;
        if (sequences.current.has(parsed.sequence)) return;
        sequences.current.add(parsed.sequence);
        setEvents((current) => [...current, parsed].sort((a, b) => a.sequence - b.sequence).slice(-500));
        if (message.lastEventId) setLastEventId(message.lastEventId);
      } catch (reason) {
        setError(reason instanceof Error ? reason : new Error("无法解析评测事件"));
      }
    };
    source.addEventListener("evaluation-event", onEvent);
    source.addEventListener("message", onEvent);
    return () => {
      source.removeEventListener("evaluation-event", onEvent);
      source.removeEventListener("message", onEvent);
      source.close();
      setConnection("CLOSED");
    };
  }, [factory, taskId]);

  return useMemo(
    () => ({ connection, events, lastEventId, error }),
    [connection, events, lastEventId, error],
  );
}
