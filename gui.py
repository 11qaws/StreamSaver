import logging
import os
import threading
import queue

logger = logging.getLogger("StreamSaver.GUI")


class GUIManager:
    def __init__(self):
        self._msg_queue = queue.Queue()
        self._running = False

    def notify(self, title, message):
        try:
            from win10toast import ToastNotifier
            toaster = ToastNotifier()
            toaster.show_toast(title, message, duration=5, threaded=True)
        except Exception as e:
            logger.debug(f"Toast not available: {e}")

    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            def _run():
                w, h = 16, 16
                img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse([2, 2, w - 2, h - 2], fill=(0, 200, 80))
                icon = pystray.Icon(
                    "streamsaver",
                    img,
                    "StreamSaver",
                    menu=pystray.Menu(
                        pystray.MenuItem("종료", self._on_quit),
                    ),
                )
                self._running = True
                icon.run()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
        except Exception as e:
            logger.debug(f"Tray not available: {e}")

    def _on_quit(self, icon):
        icon.stop()
        self._running = False
        logger.info("Tray quit requested")
        os._exit(0)

    def show_status(self):
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title("StreamSaver")
            root.geometry("400x300")
            root.configure(bg="#1e1e1e")

            style = ttk.Style()
            style.theme_use("clam")
            style.configure("TLabel", background="#1e1e1e",
                            foreground="#ffffff")
            style.configure("TFrame", background="#1e1e1e")

            ttk.Label(root, text="StreamSaver 실행 중",
                      font=("Arial", 14)).pack(pady=20)

            status_text = tk.Text(root, bg="#2d2d2d", fg="#ffffff",
                                  insertbackground="white", height=12)
            status_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            def poll():
                try:
                    while True:
                        msg = self._msg_queue.get_nowait()
                        status_text.insert(tk.END, msg + "\n")
                        status_text.see(tk.END)
                except queue.Empty:
                    pass
                root.after(1000, poll)

            root.after(1000, poll)
            root.mainloop()
        except Exception as e:
            logger.debug(f"Status window not available: {e}")

    def post_message(self, msg):
        self._msg_queue.put(msg)
