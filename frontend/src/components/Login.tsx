import { FormEvent, useState } from "react";
import { api, setToken } from "../api";
import type { User } from "../types";

interface TokenResponse {
  access_token: string;
  user: User;
}

export default function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await api<TokenResponse>("/auth/login", { method: "POST", body: { email, password } });
      setToken(res.access_token);
      onLogin(res.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось войти");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <h1>OperDir</h1>
        <p className="muted">Вход в рабочую систему</p>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required />
        </label>
        <label>
          Пароль
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <div className="error">{error}</div>}
        <button type="submit" disabled={busy}>
          {busy ? "Входим…" : "Войти"}
        </button>
      </form>
    </div>
  );
}
