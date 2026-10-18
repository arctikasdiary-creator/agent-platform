"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError, api, getToken, storeSession } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      return;
    }
    api
      .me()
      .then(() => router.replace("/tasks"))
      .catch(() => undefined);
  }, [router]);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const session = await api.login(email.trim(), password);
      storeSession(session.access_token, session.user);
      router.replace("/tasks");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Неверный email или пароль");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Сервер недоступен");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-wrap">
      <form className="card login-card" onSubmit={onSubmit}>
        <h1>Agent Platform</h1>
        <p className="muted">Войдите, чтобы управлять задачами</p>
        <label className="field">
          <span>Email</span>
          <input
            className="input"
            type="email"
            required
            value={email}
            autoComplete="username"
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <label className="field">
          <span>Пароль</span>
          <input
            className="input"
            type="password"
            required
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error ? <p className="error-text">{error}</p> : null}
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "Входим..." : "Войти"}
        </button>
      </form>
    </main>
  );
}
