import { FormEvent, useEffect, useState } from "react";
import { api } from "../../api";

interface Llm {
  provider: "anthropic" | "openai";
  models: Record<string, string>;
  suggestions: Record<string, string[]>;
  has_key: Record<string, boolean>;
}

const NAMES = { anthropic: "Claude (Anthropic)", openai: "GPT (OpenAI)" } as const;

/** Выбор модели ИИ: Claude или GPT и версия. Ключи добавляются в «Подключения». */
export default function ModelPanel({ onAddKey, onChecked }: { onAddKey: (kind: string) => void; onChecked: () => void }) {
  const [data, setData] = useState<Llm | null>(null);
  const [provider, setProvider] = useState<"anthropic" | "openai">("anthropic");
  const [model, setModel] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Llm>("/llm")
      .then((d) => {
        setData(d);
        setProvider(d.provider);
        setModel(d.models[d.provider]);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, []);

  if (error && !data) return <div className="error">{error}</div>;
  if (!data) return <p className="muted">Загрузка…</p>;

  function pick(p: "anthropic" | "openai") {
    setProvider(p);
    setModel(data!.models[p]);
    setMsg("");
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setMsg("");
    try {
      const d = await api<Llm>("/llm", { method: "PUT", body: { provider, model } });
      setData(d);
      await api("/integrations/llm/check", { method: "POST" });
      onChecked();
      setMsg(`Сохранено. Теперь отвечает ${NAMES[d.provider]}, модель ${d.models[d.provider]}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="model-panel" onSubmit={save}>
      <p className="muted">Эта модель отвечает в чате, работает в агентах и разбирает видео и документы. Можно переключаться в любой момент: история чата и данные сохраняются.</p>
      <div className="model-choice">
        {(["anthropic", "openai"] as const).map((p) => (
          <label key={p} className={`model-card${provider === p ? " on" : ""}`}>
            <input type="radio" name="provider" checked={provider === p} onChange={() => pick(p)} />
            <strong>{NAMES[p]}</strong>
            <span className="muted small">{data.has_key[p] ? "Ключ добавлен" : "Ключ не добавлен"}</span>
            {!data.has_key[p] && (
              <button type="button" className="ghost" onClick={() => onAddKey(p)}>Добавить ключ</button>
            )}
          </label>
        ))}
      </div>
      <label className="model-field">
        Версия модели
        <select
          value={data.suggestions[provider].includes(model) ? model : "__custom"}
          onChange={(e) => setModel(e.target.value === "__custom" ? "" : e.target.value)}
        >
          {data.suggestions[provider].map((m) => <option key={m} value={m}>{m}</option>)}
          <option value="__custom">Другая (вписать название)</option>
        </select>
        {!data.suggestions[provider].includes(model) && (
          <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="Точное название модели" autoFocus />
        )}
        <span className="muted small">Выберите версию из списка или впишите точное название модели из кабинета провайдера.</span>
      </label>
      {!data.has_key[provider] && <div className="error">Для выбранной модели не добавлен ключ: без него ИИ не ответит.</div>}
      {error && <div className="error">{error}</div>}
      {msg && <p className="ok-text">{msg}</p>}
      <button className="primary" disabled={busy || !model.trim()}>{busy ? "Сохраняем…" : "Сохранить и проверить"}</button>
    </form>
  );
}
