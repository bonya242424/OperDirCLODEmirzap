import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatMessage } from "../types";

export default function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<ChatMessage[]>("/chat/history")
      .then(setMessages)
      .catch(() => {});
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, busy]);

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true);
    setError("");
    const draft: ChatMessage = { id: -Date.now(), role: "user", content: message, created_at: new Date().toISOString() };
    setMessages((prev) => [...prev, draft]);
    setText("");
    try {
      const res = await api<{ reply: ChatMessage }>("/chat", { method: "POST", body: { message } });
      setMessages((prev) => [...prev, res.reply]);
    } catch (err) {
      // Сообщение не сохранилось на сервере: убираем его и возвращаем текст в поле ввода.
      setMessages((prev) => prev.filter((m) => m.id !== draft.id));
      setText(message);
      setError(err instanceof Error ? err.message : "Не удалось отправить");
    } finally {
      setBusy(false);
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <div className="chat">
      <div className="chat-log">
        {messages.length === 0 && (
          <div className="chat-empty">
            Спросите что-нибудь. Пока я не подключён к показателям, задачам и файлам компании, поэтому цифр по
            бизнесу назвать не смогу.
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`bubble ${m.role}`}>
            {m.content}
          </div>
        ))}
        {busy && <div className="bubble assistant pending">Думаю…</div>}
        <div ref={endRef} />
      </div>
      {error && <div className="error chat-error">{error}</div>}
      <form className="chat-input" onSubmit={send}>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder="Сообщение (Enter — отправить, Shift+Enter — новая строка)"
          rows={3}
        />
        <button type="submit" disabled={busy || !text.trim()}>
          Отправить
        </button>
      </form>
    </div>
  );
}
