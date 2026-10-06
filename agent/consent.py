"""Visible Windows user consent and active-session notice for remote support."""

import queue
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ConsentEvent:
    kind: str
    request_id: str | None = None


class ConsentController:
    def __init__(self) -> None:
        self._events: queue.Queue[ConsentEvent] = queue.Queue(maxsize=8)
        self._lock = threading.Lock()
        self._request_id: str | None = None
        self._stop_ui = threading.Event()
        self._thread: threading.Thread | None = None

    def request(self, request_id: str, technician_name: str, hostname: str, expires_in: int) -> bool:
        with self._lock:
            if self._request_id is not None:
                return False
            self._request_id = request_id
            self._stop_ui.clear()
        self._thread = threading.Thread(
            target=self._run_ui,
            args=(request_id, technician_name, hostname, max(1, expires_in)),
            daemon=True,
            name="prefeitura-remote-consent",
        )
        self._thread.start()
        return True

    def activate(self, request_id: str) -> bool:
        with self._lock:
            return self._request_id == request_id

    def stop(self) -> None:
        with self._lock:
            self._request_id = None
        self._stop_ui.set()

    def poll(self) -> ConsentEvent | None:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def _emit(self, kind: str, request_id: str | None = None) -> None:
        try:
            self._events.put_nowait(ConsentEvent(kind, request_id))
        except queue.Full:
            pass

    def _run_ui(self, request_id: str, technician_name: str, hostname: str, expires_in: int) -> None:
        try:
            import tkinter as tk
        except ImportError:
            self._emit("reject", request_id)
            return

        root = None
        answered = False
        try:
            root = tk.Tk()
            root.title("Prefeitura Remote")
            root.resizable(False, False)
            root.attributes("-topmost", True)

            frame = tk.Frame(root, padx=24, pady=20)
            frame.pack()
            title = tk.Label(frame, text="Solicitação de acesso remoto", font=("Segoe UI", 15, "bold"))
            title.pack(pady=(0, 10))
            message = tk.Label(frame, justify="center", wraplength=430, font=("Segoe UI", 10))
            message.pack(pady=(0, 12))
            countdown = tk.Label(frame, font=("Segoe UI", 9))
            countdown.pack(pady=(0, 12))
            buttons = tk.Frame(frame)
            buttons.pack()

            def destroy() -> None:
                try:
                    root.destroy()
                except Exception:
                    pass

            def reject() -> None:
                nonlocal answered
                if answered:
                    return
                answered = True
                self._emit("reject", request_id)
                destroy()

            def accept() -> None:
                nonlocal answered
                if answered:
                    return
                answered = True
                self._emit("accept", request_id)
                title.config(text="Acesso remoto em andamento")
                message.config(
                    text=(
                        f"O técnico {technician_name}, da TI, está acessando este computador.\n\n"
                        f"Computador: {hostname}\n\n"
                        "Sua tela está sendo compartilhada."
                    )
                )
                countdown.config(text="Você pode encerrar o acesso a qualquer momento.")
                for child in buttons.winfo_children():
                    child.destroy()
                tk.Button(buttons, text="Encerrar acesso", width=18, command=user_stop).pack()

            def user_stop() -> None:
                self._emit("user_stop", request_id)
                destroy()

            root.protocol("WM_DELETE_WINDOW", reject)
            message.config(
                text=(
                    f"O técnico {technician_name}, da TI, está solicitando acesso remoto a este computador.\n\n"
                    f"Computador: {hostname}"
                )
            )
            tk.Button(buttons, text="Recusar", width=12, command=reject).pack(side="left", padx=6)
            tk.Button(buttons, text="Aceitar acesso", width=16, command=accept).pack(side="left", padx=6)

            deadline = time.monotonic() + expires_in

            def tick() -> None:
                if self._stop_ui.is_set():
                    destroy()
                    return
                if not answered:
                    remaining = max(0, int(deadline - time.monotonic() + 0.999))
                    countdown.config(text=f"Esta solicitação expira em {remaining}s")
                    if remaining <= 0:
                        reject()
                        return
                root.after(250, tick)

            root.after(0, tick)
            root.mainloop()
        except Exception:
            if not answered:
                self._emit("reject", request_id)
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
        finally:
            with self._lock:
                if self._request_id == request_id:
                    self._request_id = None
            self._stop_ui.clear()
