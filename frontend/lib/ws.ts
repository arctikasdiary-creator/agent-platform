import type { EventItem, Task, WsMessage } from "./types";

export interface TaskSocketHandlers {
  onSnapshot?: (task: Task, events: EventItem[]) => void;
  onEvent?: (event: EventItem) => void;
  onStateChange?: (state: "connecting" | "open" | "closed") => void;
}

export interface TaskSocket {
  close: () => void;
}

export function openTaskSocket(url: string, handlers: TaskSocketHandlers): TaskSocket {
  let closedByUser = false;
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let attempts = 0;

  const connect = () => {
    handlers.onStateChange?.("connecting");
    socket = new WebSocket(url);

    socket.onopen = () => {
      attempts = 0;
      handlers.onStateChange?.("open");
    };

    socket.onmessage = (message) => {
      try {
        const data = JSON.parse(String(message.data)) as WsMessage;
        if (data.type === "snapshot") {
          handlers.onSnapshot?.(data.task, data.events);
        } else if (data.type === "event") {
          handlers.onEvent?.(data.event);
        }
      } catch {
        return;
      }
    };

    socket.onclose = () => {
      handlers.onStateChange?.("closed");
      if (!closedByUser) {
        attempts += 1;
        const delay = Math.min(1000 * 2 ** Math.min(attempts, 4), 15000);
        retryTimer = setTimeout(connect, delay);
      }
    };

    socket.onerror = () => {
      socket?.close();
    };
  };

  connect();

  return {
    close: () => {
      closedByUser = true;
      if (retryTimer) {
        clearTimeout(retryTimer);
      }
      socket?.close();
    },
  };
}
