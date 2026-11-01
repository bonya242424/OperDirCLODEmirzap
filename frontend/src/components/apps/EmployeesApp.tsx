import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { User } from "../../types";

const EMPTY = { email: "", password: "", full_name: "", position: "", department: "" };

export default function EmployeesApp({ me }: { me: User }) {
  const [users, setUsers] = useState<User[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const isOwner = me.role === "owner";

  const load = useCallback(() => {
    api<User[]>("/users")
      .then(setUsers)
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api<User>("/users", { method: "POST", body: form });
      setForm(EMPTY);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось добавить");
    } finally {
      setBusy(false);
    }
  }

  async function update(user: User, change: Partial<User>) {
    setError("");
    try {
      await api<User>(`/users/${user.id}`, { method: "PATCH", body: change });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить");
    }
  }

  const set = (field: keyof typeof EMPTY) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  return (
    <div className="app-pad">
      <h2>Сотрудники</h2>
      {error && <div className="error">{error}</div>}
      <table className="table">
        <thead>
          <tr>
            <th>Сотрудник</th>
            <th>Должность</th>
            <th>Отдел</th>
            <th>Может применять доработки</th>
            <th>Доступ</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id} className={u.is_active ? "" : "inactive"}>
              <td>
                {u.full_name || "—"}
                <div className="muted">{u.email}</div>
              </td>
              <td>{u.position || "—"}</td>
              <td>{u.department || "—"}</td>
              <td>
                <input
                  type="checkbox"
                  checked={u.can_apply_changes}
                  disabled={!isOwner || u.role === "owner"}
                  onChange={(e) => update(u, { can_apply_changes: e.target.checked })}
                />
                {u.role === "owner" && <span className="muted"> владелец</span>}
              </td>
              <td>
                {isOwner ? (
                  <button onClick={() => update(u, { is_active: !u.is_active })}>
                    {u.is_active ? "Отключить" : "Включить"}
                  </button>
                ) : u.is_active ? (
                  "Активен"
                ) : (
                  "Отключён"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Добавить сотрудника</h3>
      <form className="form-grid" onSubmit={add}>
        <input placeholder="Email" type="email" value={form.email} onChange={set("email")} required />
        <input placeholder="Пароль (от 8 символов)" type="password" value={form.password} onChange={set("password")} required />
        <input placeholder="ФИО" value={form.full_name} onChange={set("full_name")} />
        <input placeholder="Должность" value={form.position} onChange={set("position")} />
        <input placeholder="Отдел" value={form.department} onChange={set("department")} />
        <button type="submit" disabled={busy}>
          Добавить
        </button>
      </form>
    </div>
  );
}
