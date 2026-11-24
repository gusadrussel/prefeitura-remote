"""Vyntra Windows status, session controls and support chat."""

from __future__ import annotations

import gc
import logging
import platform
from pathlib import Path
import queue
import sys
import threading
from urllib.parse import urlsplit

DIAGNOSTICS = logging.getLogger("prefeitura_remote.diagnostics")
BACKGROUND = "#0B1120"
SURFACE = "#111827"
TEXT = "#FFFFFF"
MUTED = "#94A3B8"
ACCENT = "#635BFF"
CONNECTED = "#22C55E"


def icon_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / "installer" / "cliente" / "Vyntra.ico"


class ClientApp:
    def __init__(self, agent_id: str = "", server_url: str = "") -> None:
        self._agent_id = agent_id
        self._server = urlsplit(server_url)
        self._lock = threading.Lock()
        self._updates: dict = {}
        self._incoming: queue.Queue[str] = queue.Queue(maxsize=32)
        self._outgoing: queue.Queue[str] = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._shutdown = threading.Event()
        self._ready = False
        self.closed = threading.Event()
        self.end_session = threading.Event()

    def start(self, inventory: dict) -> bool:
        if self._thread and self._thread.is_alive():
            return self._ready
        if self.closed.is_set():
            return False
        self._thread = threading.Thread(target=self._run, args=(inventory,), daemon=True, name="vyntra-client")
        self._thread.start()
        if not self._started.wait(timeout=10):
            self.stop()
            return False
        return self._ready

    def update(self, inventory: dict, status: str) -> None:
        self._post("inventory", inventory)
        if status:
            self.status(status)

    def status(self, status: str) -> None:
        self._post("connection", status)

    def receive_chat(self, message: str) -> None:
        try:
            self._incoming.put_nowait(message)
        except queue.Full:
            DIAGNOSTICS.warning("Fila de mensagens da interface cheia")

    def session_status(self, status: str, *, active: bool = False) -> None:
        self._post("session", (status, active))

    def request_status(self, pending: bool) -> None:
        self._post("pending", pending)

    def next_chat(self) -> str | None:
        try:
            return self._outgoing.get_nowait()
        except queue.Empty:
            return None

    def _post(self, kind: str, value) -> None:
        with self._lock:
            self._updates[kind] = value

    def stop(self) -> None:
        self._shutdown.set()

    def _run(self, inventory: dict) -> None:
        try:
            self._run_window(inventory)
        except Exception:
            DIAGNOSTICS.exception("Falha na interface do Vyntra")
        finally:
            self.closed.set()
            self._started.set()
            gc.collect()

    def _run_window(self, inventory: dict) -> None:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        tray = None
        try:
            root.title("Vyntra")
            root.configure(bg=BACKGROUND)
            root.geometry("505x592")
            root.minsize(455, 560)
            root.report_callback_exception = lambda kind, error, trace: DIAGNOSTICS.error(
                "Falha em ação do Vyntra", exc_info=(kind, error, trace)
            )
            if sys.platform == "win32":
                root.iconbitmap(default=str(icon_path()))

            def label(parent, text="", *, bg=SURFACE, fg=TEXT, size=10, bold=False):
                return tk.Label(parent, text=text, bg=bg, fg=fg, anchor="w",
                                font=("Segoe UI", size, "bold" if bold else "normal"))

            def button(parent, text, command, *, color=ACCENT):
                return tk.Button(parent, text=text, command=command, bg=color, fg=TEXT,
                                 activebackground="#4941bf", activeforeground=TEXT,
                                 disabledforeground=MUTED, relief="flat", padx=11, pady=4,
                                 font=("Segoe UI", 9, "bold"))

            header = tk.Frame(root, bg=BACKGROUND, padx=20, pady=12)
            header.pack(fill="x")
            if icon_path().is_file():
                from PIL import Image, ImageTk
                with Image.open(icon_path()) as icon:
                    mark = ImageTk.PhotoImage(icon.resize((36, 36)), master=root)
                tk.Label(header, image=mark, bg=BACKGROUND).pack(side="left", padx=(0, 10))
            label(header, "Vyntra", bg=BACKGROUND, size=20, bold=True).pack(side="left")
            connection = label(header, "Conectando…", bg=BACKGROUND, fg=MUTED, bold=True)
            connection.pack(side="right")

            body = tk.Frame(root, bg=BACKGROUND, padx=20)
            body.pack(fill="both", expand=True)
            device = tk.Frame(body, bg=SURFACE, padx=12, pady=8)
            device.pack(fill="x", pady=(0, 8))
            label(device, "Este computador", fg=MUTED).pack(anchor="w")
            hostname = label(device, platform.node() or "Não informado", size=15, bold=True)
            hostname.pack(anchor="w")
            public_id = self._agent_id or inventory.get("agent_id") or ""
            id_line = tk.Frame(device, bg=SURFACE)
            id_line.pack(fill="x")
            label(id_line, public_id or "ID indisponível", fg=MUTED, size=9).pack(side="left")

            def copy_id():
                root.clipboard_clear()
                root.clipboard_append(public_id)

            if public_id:
                button(id_line, "Copiar ID", copy_id, color="#273449").pack(side="right")
            security = label(device, "Canal seguro · aguardando conexão", fg=MUTED, size=9)
            security.pack(anchor="w")

            session_card = tk.Frame(body, bg=SURFACE, padx=12, pady=8)
            session_card.pack(fill="x", pady=(0, 8))
            session = label(session_card, "Compartilhamento inativo", bold=True)
            session.pack(side="left")
            active = connected = pending = False
            current_session = "Compartilhamento inativo"

            def show_window():
                root.deiconify()
                root.lift()

            def end():
                if active:
                    self.end_session.set()
                    end_button.config(state="disabled")

            end_button = button(session_card, "Encerrar sessão", end, color="#a52a43")
            end_button.pack(side="right")
            end_button.config(state="disabled")

            settings = tk.Frame(body, bg=SURFACE, padx=12, pady=8)
            settings.pack(fill="x", pady=(0, 8))
            label(settings, "Configurações", bold=True).pack(anchor="w")
            label(settings, f"Servidor · {self._server.hostname or 'Não configurado'}", fg=MUTED, size=9).pack(anchor="w")
            minimize = tk.BooleanVar(root, True)
            minimize_option = tk.Checkbutton(settings, text="Minimizar para a bandeja ao fechar a janela",
                                            variable=minimize, bg=SURFACE, fg=TEXT, selectcolor=BACKGROUND,
                                            activebackground=SURFACE, activeforeground=TEXT)

            chat = tk.Frame(body, bg=SURFACE, padx=12, pady=8)
            chat.pack(fill="both", expand=True, pady=(0, 12))
            label(chat, "Chat com a TI", bold=True).pack(anchor="w", pady=(0, 4))
            composer = tk.Frame(chat, bg=SURFACE)
            composer.pack(side="bottom", fill="x", pady=(4, 0))
            entry = tk.Entry(composer, bg=BACKGROUND, fg=TEXT, insertbackground=TEXT, relief="flat")
            entry.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=5)
            feedback = label(chat, bg=SURFACE, fg=MUTED, size=9)
            feedback.pack(side="bottom", anchor="w")
            history = tk.Text(chat, height=3, wrap="word", state="disabled",
                              bg=BACKGROUND, fg=TEXT, relief="flat", padx=7, pady=5)
            history.pack(fill="both", expand=True)

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
                    feedback.config(text="Conecte-se e digite uma mensagem de até 2.000 caracteres.")
                    return
                try:
                    self._outgoing.put_nowait(message)
                except queue.Full:
                    feedback.config(text="A fila está cheia. Tente novamente.")
                    return
                entry.delete(0, "end")
                append_chat("Você", message)
                feedback.config(text="Mensagem na fila de envio.")

            button(composer, "Enviar", send_chat).pack(side="right")
            entry.bind("<Return>", send_chat)

            def exit_app():
                if (active or pending) and not messagebox.askyesno(
                    "Vyntra", "Sair do Vyntra e encerrar o acesso remoto?", parent=root
                ):
                    return
                self.closed.set()
                self.stop()

            def close_window():
                if active or pending:
                    show_window()
                elif tray and minimize.get():
                    root.withdraw()
                else:
                    exit_app()

            def keep_visible(event):
                if event.widget is root and (active or pending):
                    root.after_idle(show_window)

            if sys.platform == "win32":
                try:
                    from agent.windows_tray import WindowsTray
                    tray = WindowsTray(root, icon_path(), lambda: [
                        ("Abrir o Vyntra", show_window),
                        (connection.cget("text"), None),
                        (session.cget("text"), None),
                        ("Encerrar sessão", end if active and not self.end_session.is_set() else None),
                        ("Sair do Vyntra", exit_app),
                    ])
                except OSError:
                    DIAGNOSTICS.exception("Bandeja do Vyntra indisponível")
                    label(settings, "Bandeja do Windows indisponível", fg=MUTED, size=9).pack(anchor="w")
                else:
                    minimize_option.pack(anchor="w")

            self.update(inventory, "")

            def poll():
                nonlocal active, connected, pending, current_session
                if self._shutdown.is_set():
                    root.quit()
                    return
                with self._lock:
                    updates, self._updates = self._updates, {}
                if "inventory" in updates:
                    system = updates["inventory"].get("system") or {}
                    if system.get("hostname"):
                        hostname.config(text=system["hostname"])
                if "connection" in updates:
                    status = updates["connection"]
                    connected = status == "Conectado ao canal de suporte"
                    connection.config(text="Conectado" if connected else "Reconectando…" if "desconectado" in status else status,
                                      fg=CONNECTED if connected else MUTED)
                    secure = connected and self._server.scheme == "https"
                    security.config(text="Canal seguro · TLS autenticado" if secure else
                                    "Canal conectado sem TLS" if connected else "Canal seguro · aguardando conexão",
                                    fg=CONNECTED if secure else MUTED)
                if "session" in updates or "pending" in updates:
                    current_session, active = updates.get("session", (current_session, active))
                    pending = updates.get("pending", pending)
                    session.config(text="Solicitação de acesso pendente" if pending else current_session,
                                   fg="#fbbf24" if active or pending else TEXT)
                    end_button.config(state="normal" if active and not self.end_session.is_set() else "disabled")
                    if active or pending:
                        show_window()
                while True:
                    try:
                        append_chat("TI", self._incoming.get_nowait())
                    except queue.Empty:
                        break
                root.after(100, poll)

            root.protocol("WM_DELETE_WINDOW", close_window)
            root.bind("<Unmap>", keep_visible)
            self._ready = True
            self._started.set()
            root.after(0, poll)
            root.mainloop()
        finally:
            if tray:
                tray.close()
            root.destroy()
