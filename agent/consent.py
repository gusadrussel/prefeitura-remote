"""Visible Windows user consent and active-session overlay for remote support."""

import gc
import logging
import queue
import threading
import time
from dataclasses import dataclass

DIAGNOSTICS = logging.getLogger("prefeitura_remote.diagnostics")


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
        self._accepted = False
        self._approved = False
        self._deadline = 0.0
        self._mode: str | None = None

    def request(self, request_id: str, technician_name: str, mode: str, hostname: str, expires_in: int) -> bool:
        with self._lock:
            if self._request_id is not None or (self._thread is not None and self._thread.is_alive()):
                return False
            self._request_id = request_id
            self._accepted = self._approved = False
            self._mode = mode
            self._deadline = time.monotonic() + max(1, expires_in)
            self._stop_ui.clear()
        self._thread = threading.Thread(
            target=self._run_ui,
            args=(request_id, technician_name, mode, hostname, max(1, expires_in)),
            daemon=True,
            name="prefeitura-remote-consent",
        )
        self._thread.start()
        return True

    def activate(self, request_id: str) -> bool:
        with self._lock:
            if self._request_id != request_id or not self._accepted or self._deadline <= time.monotonic():
                return False
            self._approved = True
            return True

    def is_active(self, mode: str) -> bool:
        with self._lock:
            return bool(self._request_id and self._approved and self._accepted and self._mode == mode)

    def matches(self, request_id: str) -> bool:
        with self._lock:
            return self._request_id == request_id

    def stop(self) -> None:
        with self._lock:
            self._request_id = None
            self._accepted = self._approved = False
        self._stop_ui.set()

    def poll(self) -> ConsentEvent | None:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def _emit(self, kind: str, request_id: str | None = None) -> None:
        with self._lock:
            if request_id != self._request_id:
                return
            if kind == "accept":
                if self._deadline <= time.monotonic():
                    kind = "reject"
                else:
                    self._accepted = True
            if kind in {"reject", "user_stop"}:
                self._approved = self._accepted = False
        try:
            self._events.put_nowait(ConsentEvent(kind, request_id))
        except queue.Full:
            self.stop()

    @staticmethod
    def _position_top_center(root, width: int, height: int, top: int = 24) -> None:
        root.update_idletasks()
        screen_width = root.winfo_screenwidth()
        x = max(8, (screen_width - width) // 2)
        root.geometry(f"{width}x{height}+{x}+{top}")

    def _run_ui(self, request_id: str, technician_name: str, mode: str, hostname: str, expires_in: int) -> None:
        try:
            self._show_consent(request_id, technician_name, mode, hostname, expires_in)
        finally:
            # Release Tk callback cycles before the owning thread exits.
            gc.collect()

    def _show_consent(self, request_id: str, technician_name: str, mode: str, hostname: str, expires_in: int) -> None:
        try:
            import tkinter as tk
        except ImportError:
            DIAGNOSTICS.warning("Interface de consentimento indisponível")
            self._emit("reject", request_id)
            self.stop()
            return

        root = None
        answered = False
        try:
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg="#163b2e")
            self._position_top_center(root, 500, 170, 190 if mode == "control" else 22)

            card = tk.Frame(root, bg="#ffffff", padx=22, pady=18)
            card.pack(fill="both", expand=True, padx=2, pady=2)
            title = tk.Label(card, text="Solicitação de suporte remoto", bg="#ffffff", fg="#17352b", font=("Segoe UI", 14, "bold"))
            title.pack(anchor="w")
            message = tk.Label(card, bg="#ffffff", fg="#596861", justify="left", anchor="w", wraplength=450, font=("Segoe UI", 9))
            message.pack(fill="x", pady=(7, 8))
            countdown = tk.Label(card, bg="#ffffff", fg="#75837c", font=("Segoe UI", 8))
            countdown.pack(anchor="w")
            buttons = tk.Frame(card, bg="#ffffff")
            buttons.pack(anchor="e", pady=(10, 0))

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

            def user_stop() -> None:
                nonlocal answered
                if answered:
                    answered = True
                self._emit("user_stop", request_id)
                destroy()

            def accept() -> None:
                nonlocal answered
                if answered:
                    return
                answered = True
                self._emit("accept", request_id)
                for child in buttons.winfo_children():
                    child.destroy()
                title.config(text="Aguardando confirmação do canal")
                message.config(text="Você autorizou esta solicitação. Pode cancelar a qualquer momento.")
                countdown.config(text="")
                stop_btn = tk.Button(
                    buttons, text="Encerrar acesso", command=user_stop,
                    bg="#a44a44", fg="#ffffff", activebackground="#8e3934", activeforeground="#ffffff",
                    relief="flat", bd=0, padx=16, pady=7, font=("Segoe UI", 9, "bold"), cursor="hand2",
                )
                stop_btn.pack()
                self._position_top_center(root, 500, 150, 190 if mode == "control" else 22)

            action = "gerenciar a tela" if mode == "control" else "visualizar a tela"
            message.config(text=f"{technician_name}, da TI, está solicitando permissão para {action}.\n\nComputador: {hostname}")
            tk.Button(
                buttons, text="Recusar", command=reject,
                bg="#ffffff", fg="#5c6963", activebackground="#eef2ef", relief="flat",
                highlightthickness=1, highlightbackground="#d5dfda", bd=0, padx=16, pady=7,
                font=("Segoe UI", 9, "bold"), cursor="hand2",
            ).pack(side="left", padx=(0, 8))
            tk.Button(
                buttons, text="Aceitar acesso", command=accept,
                bg="#174f3d", fg="#ffffff", activebackground="#103a2d", activeforeground="#ffffff",
                relief="flat", bd=0, padx=16, pady=7, font=("Segoe UI", 9, "bold"), cursor="hand2",
            ).pack(side="left")

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
                elif self.is_active(mode):
                    title.config(text="Controle remoto autorizado" if mode == "control" else "Visualização remota autorizada")
                    message.config(text=f"{technician_name}, da TI, tem sua autorização nesta sessão.")
                elif time.monotonic() >= deadline:
                    user_stop()
                    return
                root.after(250, tick)

            root.protocol("WM_DELETE_WINDOW", reject)
            root.after(0, tick)
            root.mainloop()
        except Exception:
            DIAGNOSTICS.warning("Falha na interface de consentimento")
            if not answered:
                self._emit("reject", request_id)
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
        finally:
            if answered and self.is_active(mode):
                self._emit("user_stop", request_id)
            with self._lock:
                if self._request_id == request_id:
                    self._request_id = None
                    self._approved = self._accepted = False
