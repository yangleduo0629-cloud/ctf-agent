import React, { useEffect, useState } from "react";

const apiBase = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8080";

export default function App() {
  const [state, setState] = useState({ label: "正在连接", tone: "checking" });

  useEffect(() => {
    fetch(`${apiBase}/readyz`)
      .then((response) => {
        if (!response.ok) throw new Error("API readiness check failed");
        return response.json();
      })
      .then(() => setState({ label: "基础服务就绪", tone: "ready" }))
      .catch(() => setState({ label: "基础服务检查中", tone: "checking" }));
  }, []);

  return (
    <main>
      <header>
        <span className="mark" aria-hidden="true">CTF</span>
        <div>
          <h1>CTF Platform</h1>
          <p>本地工作区</p>
        </div>
      </header>
      <section aria-label="服务状态">
        <div className={`status ${state.tone}`} aria-hidden="true" />
        <div>
          <strong>{state.label}</strong>
          <span>API · PostgreSQL · Redis</span>
        </div>
      </section>
    </main>
  );
}
