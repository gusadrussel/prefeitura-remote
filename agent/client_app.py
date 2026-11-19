"""Visible Windows client status and support chat."""
from __future__ import annotations

import gc
import queue
import threading
from typing import Any


class ClientApp:
    def __init__(self) -> None:
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self.closed = threading.Event()
        self._outgoing: queue.Queue[str] = queue.Queue(maxsize=8)
        self._ready = False

    def start(self, inventory: dict) -> bool:
        if self._thread and self._thread.is_alive():
            return self._ready
        self._thread = threading.Thread(target=self._run, args=(inventory,), daemon=True, name="prefeitura-remote-client")
        self._thread.start()
        self._started.wait(timeout=3)
        return self._ready

    def update(self, inventory: dict, status: str) -> None:
        self._post("update", (inventory, status))

    def status(self, status: str) -> None:
        self._post("status", status)

    def receive_chat(self, message: str) -> None:
        self._post("chat", message)

    def session_status(self, status: str) -> None:
        self._post("session", status)

    def next_chat(self) -> str | None:
        try:
            return self._outgoing.get_nowait()
        except queue.Empty:
            return None

    def _post(self, kind: str, value: Any) -> None:
        try:
            self._events.put_nowait((kind, value))
        except queue.Full:
            pass

    def stop(self) -> None:
        try:
            self._events.put_nowait(("stop", None))
        except queue.Full:
            pass

    def _run(self, inventory: dict) -> None:
        try:
            self._run_window(inventory)
        finally:
            # Release Tk callback cycles on the thread that owns Tcl.
            gc.collect()
            self.closed.set()

    def _run_window(self, inventory: dict) -> None:
        try:
            import tkinter as tk
        except ImportError:
            self.closed.set()
            self._started.set()
            return
        try:
            root = tk.Tk()
            root.title("Prefeitura Remote")
            root.resizable(False, False)
            root.configure(bg="#f4f6f3")
            root.geometry("460x650")

            header = tk.Frame(root, bg="#103a2d", padx=20, pady=16)
            header.pack(fill="x")
            tk.Label(header, text="Prefeitura Remote", bg="#103a2d", fg="#ffffff", font=("Segoe UI", 14, "bold")).pack(anchor="w")
            tk.Label(header, text="Informações deste computador", bg="#103a2d", fg="#cfe4d9", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

            body = tk.Frame(root, bg="#f4f6f3", padx=20, pady=18)
            body.pack(fill="both", expand=True)
            status_label = tk.Label(body, text="Conectando…", bg="#f4f6f3", fg="#176144", font=("Segoe UI", 9, "bold"))
            status_label.pack(anchor="w", pady=(0, 12))
            session_label = tk.Label(body, text="Compartilhamento inativo", bg="#f4f6f3", fg="#176144")
            session_label.pack(anchor="w")
            rows: list[tk.Label] = []
            for _ in range(6):
                label = tk.Label(body, bg="#ffffff", fg="#192622", anchor="w", padx=12, pady=8, font=("Segoe UI", 9))
                label.pack(fill="x", pady=2)
                rows.append(label)

            tk.Label(body, text="O acesso remoto só começa após sua confirmação.", bg="#f4f6f3", fg="#65726c", font=("Segoe UI", 8)).pack(anchor="w", pady=(12, 0))
            tk.Label(body, text="Chat com a TI", bg="#f4f6f3", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 0))
            history = tk.Text(body, height=5, wrap="word", state="disabled")
            history.pack(fill="x")
            composer = tk.Frame(body, bg="#f4f6f3")
            composer.pack(fill="x")
            entry = tk.Entry(composer)
            entry.pack(side="left", fill="x", expand=True)
            feedback = tk.Label(body, bg="#f4f6f3")
            feedback.pack(anchor="w")
            connected = False

            def append_chat(sender, message):
                history.config(state="normal")
                history.insert("end", f"{sender}: {message}\n")
                if int(history.index("end-1c").split(".")[0]) > 200:
                    history.delete("1.0", "2.0")
                history.see("end")
                history.config(state="disabled")

            def send_chat(event=None):
                message = entry.get().strip()
                if not connected or not 1 <= len(message) <= 2000 or "\x00" in message:
                    feedback.config(text="Aguarde a conexão e informe uma mensagem de até 2.000 caracteres.")
                    return
                try:
                    self._outgoing.put_nowait(message)
                except queue.Full:
                    feedback.config(text="A fila está cheia. Aguarde e tente novamente.")
                    return
                entry.delete(0, "end")
                append_chat("Você", message)
                feedback.config(text="Mensagem na fila de envio.")

            tk.Button(composer, text="Enviar", command=send_chat).pack(side="right")
            entry.bind("<Return>", send_chat)

            def render(data: dict, status: str) -> None:
                system = data.get("system") or {}
                cpu = data.get("cpu") or {}
                memory = data.get("memory") or {}
                storage = data.get("storage") or {}
                disks = storage.get("disks") or []
                hostname = system.get("hostname") or "Não informado"
                user = system.get("logged_user") or "Não informado"
                os_name = system.get("os_name") or "Não informado"
                cpu_name = cpu.get("model") or "Não informado"
                ram = memory.get("total_bytes")
                if isinstance(ram, (int, float)):
                    ram = f"{ram / (1024 ** 3):.1f} GB"
                else:
                    ram = "Não informado"
                storage_name = ", ".join(str(d.get("media_type") or "desconhecido") for d in disks) or "Não informado"
                values = [
                    f"Computador  ·  {hostname}", f"Usuário  ·  {user}", f"Sistema  ·  {os_name}",
                    f"Processador  ·  {cpu_name}", f"Memória  ·  {ram}", f"Armazenamento  ·  {storage_name}",
                ]
                for label, value in zip(rows, values):
                    label.config(text=value)
                if status:
                    status_label.config(text=status)

            render(inventory, "Conectando…")

            def poll() -> None:
                nonlocal connected
                try:
                    while True:
                        kind, value = self._events.get_nowait()
                        if kind == "update":
                            render(*value)
                        elif kind == "status":
                            status_label.config(text=value)
                            connected = value == "Conectado ao canal de suporte"
                            if not connected:
                                while self.next_chat() is not None:
                                    pass
                        elif kind == "chat":
                            append_chat("TI", value)
                        elif kind == "session":
                            session_label.config(text=value)
                        elif kind == "stop":
                            root.destroy()
                            return
                except queue.Empty:
                    pass
                root.after(250, poll)

            def close() -> None:
                self.closed.set()
                root.destroy()

            root.protocol("WM_DELETE_WINDOW", close)
            self._ready = True
            self._started.set()
            root.after(0, poll)
            root.mainloop()
        except Exception:
            self._started.set()
