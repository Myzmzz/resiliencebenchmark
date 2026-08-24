import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useEvaluationEventStream } from "./useEvaluationEventStream";
import type { EventSourceLike } from "./useEvaluationEventStream";

class FakeEventSource implements EventSourceLike {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, Set<EventListener>>();
  closed = false;
  addEventListener(type: string, listener: EventListener) { const set = this.listeners.get(type) ?? new Set<EventListener>(); set.add(listener); this.listeners.set(type, set); }
  removeEventListener(type: string, listener: EventListener) { this.listeners.get(type)?.delete(listener); }
  close() { this.closed = true; }
  emit(type: string, data: object, lastEventId: string) { const event = new MessageEvent(type, { data: JSON.stringify(data), lastEventId }); this.listeners.get(type)?.forEach((listener) => listener(event)); }
}

describe("useEvaluationEventStream", () => {
  it("使用标准 SSE 地址并按 sequence 去重排序", async () => {
    const source = new FakeEventSource();
    let url = "";
    const factory = (value: string) => { url = value; return source; };
    const { result, unmount } = renderHook(() => useEvaluationEventStream("EVAL-1", factory));
    act(() => source.onopen?.(new Event("open")));
    expect(url).toBe("/api/v1/evaluation/tasks/EVAL-1/events");
    act(() => {
      source.emit("evaluation-event", { id: "2", sequence: 2, taskId: "EVAL-1", type: "B", phase: "EXECUTING", occurredAt: "2026-08-25T10:00:02Z", message: "二" }, "2");
      source.emit("evaluation-event", { id: "1", sequence: 1, taskId: "EVAL-1", type: "A", phase: "EXECUTING", occurredAt: "2026-08-25T10:00:01Z", message: "一" }, "1");
      source.emit("evaluation-event", { id: "2", sequence: 2, taskId: "EVAL-1", type: "B", phase: "EXECUTING", occurredAt: "2026-08-25T10:00:02Z", message: "二" }, "2");
    });
    await waitFor(() => expect(result.current.events.map((event) => event.sequence)).toEqual([1, 2]));
    expect(result.current.lastEventId).toBe("1");
    expect(result.current.connection).toBe("OPEN");
    unmount();
    expect(source.closed).toBe(true);
  });

  it("错误时显示浏览器 Last-Event-ID 自动重连状态", () => {
    const source = new FakeEventSource();
    const factory = () => source;
    const { result } = renderHook(() => useEvaluationEventStream("EVAL-1", factory));
    act(() => source.onerror?.(new Event("error")));
    expect(result.current.connection).toBe("RECONNECTING");
    expect(result.current.error?.message).toContain("Last-Event-ID");
  });
});
