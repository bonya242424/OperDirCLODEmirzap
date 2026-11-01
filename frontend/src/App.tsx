import { useCallback, useEffect, useState } from "react";
import { api, getToken, setToken, setUnauthorizedHandler } from "./api";
import Desktop from "./components/Desktop";
import Login from "./components/Login";
import type { User } from "./types";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(() => getToken() !== null);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    if (!getToken()) return;
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, [logout]);

  if (loading) return <div className="splash">Загрузка…</div>;
  if (!user) return <Login onLogin={setUser} />;
  return <Desktop user={user} onLogout={logout} />;
}
