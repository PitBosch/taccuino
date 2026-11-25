"""Finestrina "sottotitoli" sempre in primo piano: trascrizione live + riga comandi (Tkinter, zero dipendenze).

  taccuino sottotitoli

Stessi comandi della console (r 5, t, n testo, start, stop, domande libere). Si appoggia agli stessi file
della riunione: se è aperta anche la console, una sola delle due trascrive e l'altra legge.
"""
from __future__ import annotations

import contextlib
import io
import queue
import re
import threading
import time
from pathlib import Path

BG, FG, DIMC, IO, CALL, ANS, RED = "#1b1b1f", "#e8e8ea", "#8a8a93", "#f2c66d", "#6cc4e0", "#cfe8cf", "#ff6b6b"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _QueueWriter(io.TextIOBase):
    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> int:
        s = ANSI.sub("", s)
        if s.strip():
            self.q.put(("answer", s.rstrip("\n")))
        return len(s)


class Captions:
    def __init__(self):
        import tkinter as tk
        from .console import Console
        from .pipeline import fmt_ts
        self.tk, self.fmt_ts = tk, fmt_ts
        self.con = Console()            # riusa comandi, stato e logica Claude della console
        self.q: queue.Queue = queue.Queue()
        self.lt = None
        self.shown: set[int] = set()        # righe già a schermo
        self.fixed_shown: set[int] = set()  # righe già a schermo nella versione corretta
        self.draft = ""
        self.stop = threading.Event()

        r = self.root = tk.Tk()
        r.title("Taccuino")
        r.configure(bg=BG)
        r.attributes("-topmost", True)
        with contextlib.suppress(tk.TclError):
            r.attributes("-alpha", 0.93)
        w, h = 600, 230
        r.geometry(f"{w}x{h}+{r.winfo_screenwidth() - w - 30}+{r.winfo_screenheight() - h - 90}")
        r.minsize(320, 120)

        self.status = tk.Label(r, text="…", bg=BG, fg=DIMC, anchor="w", font=("TkDefaultFont", 9))
        self.status.pack(fill="x", padx=8, pady=(6, 0))
        self.text = tk.Text(r, bg=BG, fg=FG, wrap="word", bd=0, highlightthickness=0, padx=8, pady=4,
                            font=("TkDefaultFont", 11), insertbackground=FG, cursor="arrow")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("ts", foreground=DIMC, font=("TkFixedFont", 9))
        self.text.tag_configure("Io", foreground=IO)
        self.text.tag_configure("Call", foreground=CALL)
        self.text.tag_configure("draft", foreground=DIMC, font=("TkDefaultFont", 11, "italic"))
        self.text.tag_configure("raw", foreground="#b9b9c0")  # in attesa della correzione di Haiku
        self.text.tag_configure("answer", foreground=ANS, lmargin1=12, lmargin2=12)
        self.text.tag_configure("info", foreground=DIMC)
        self.text.tag_configure("cmd", foreground=DIMC)
        self.text.mark_set("draft_start", "end-1c")
        self.text.mark_gravity("draft_start", "left")
        self.text.configure(state="disabled")

        self.entry = tk.Entry(r, bg="#26262c", fg=FG, insertbackground=FG, bd=0, highlightthickness=1,
                              highlightbackground="#3a3a42", highlightcolor="#6b6bff", font=("TkDefaultFont", 11))
        self.entry.pack(fill="x", padx=8, pady=8, ipady=4)
        self.hint = "r riassumi 5 min · t trascrizione · n nota · oppure chiedi qualcosa"
        self._placeholder(True)
        self.entry.bind("<FocusIn>", lambda _: self._placeholder(False))
        self.entry.bind("<FocusOut>", lambda _: self._placeholder(not self.entry.get()))
        self.entry.bind("<Return>", self._submit)
        r.bind("<Escape>", lambda _: r.iconify())
        r.protocol("WM_DELETE_WINDOW", self.close)

    # ---------- UI ----------
    def _placeholder(self, on: bool):
        if on and not self.entry.get():
            self.entry.insert(0, self.hint)
            self.entry.configure(fg=DIMC)
        elif not on and self.entry.get() == self.hint:
            self.entry.delete(0, "end")
            self.entry.configure(fg=FG)

    def _append(self, parts: list[tuple[str, str]]):
        t = self.text
        t.configure(state="normal")
        t.delete("draft_start", "end")
        for s, tag in parts:
            t.insert("end", s, tag)
        t.mark_set("draft_start", "end-1c")
        if self.draft:
            t.insert("end", "… " + self.draft, "draft")
        t.configure(state="disabled")
        t.see("end")  # sempre sull'ultima riga

    def _fix(self, i: int, text: str):
        t = self.text
        L, T = f"L{i}", f"T{i}"
        r = t.tag_ranges(L)
        if not r:
            return
        t.configure(state="normal")
        if not text:  # allucinazione scartata da Haiku
            t.delete(r[0], r[1])
        elif (rt := t.tag_ranges(T)):
            t.delete(rt[0], rt[1])
            t.insert(rt[0], text, (T, L))
        t.configure(state="disabled")
        t.see("end")

    def _set_draft(self, d: str):
        if d != self.draft:
            self.draft = d
            self._append([])

    def _submit(self, _=None):
        line = self.entry.get().strip()
        if not line or line == self.hint:
            return
        self.entry.delete(0, "end")
        if line.lower() in ("q", "esci"):
            return self.close()
        self._append([(f"› {line}\n", "cmd")])
        self.q.put(("info", "…"))

        def run():
            self.con.live = self.lt or self.con.live  # stesso modello e stessi file del thread live
            with contextlib.redirect_stdout(_QueueWriter(self.q)):
                try:
                    self.con.handle(line)
                except SystemExit as e:
                    print(str(e))
                except Exception as e:  # noqa: BLE001
                    print(f"errore: {e}")
        threading.Thread(target=run, daemon=True).start()

    def _pump(self):
        """Gira nel thread Tk: applica gli aggiornamenti arrivati dai thread di lavoro."""
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":  # frase appena fissata: subito a schermo (grezza o già corretta)
                    i, l, ok = val
                    if l.text:
                        L, T = f"L{i}", f"T{i}"
                        self._append([(self.fmt_ts(l.start) + " ", ("ts", L)), (l.speaker, (l.speaker, L)),
                                      ("  ", L), (l.text, (T, L) if ok else (T, L, "raw")), ("\n", L)])
                elif kind == "fix":  # Haiku ha corretto la frase: sostituisci sul posto
                    i, text = val
                    self._fix(i, text)
                elif kind == "draft":
                    self._set_draft(val)
                elif kind == "status":
                    self.status.configure(text=val[0], fg=val[1])
                elif kind == "answer":
                    if val.strip() == "… Claude sta pensando":
                        val = "Claude (Haiku) sta pensando…"
                        self._append([(val + "\n", "info")])
                    else:
                        self._append([(val + "\n", "answer")])
                elif kind == "info":
                    pass
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(120, self._pump)

    # ---------- lavoro in background ----------
    def _status(self, st):
        if not st:
            return "✕ daemon spento  (taccuino install-service)", RED
        if st["recording"]:
            el = int(time.time() - st["since"])
            apps = ", ".join(a.replace(" input", "") for a in st.get("apps", [])) or "manuale"
            return f"● REC {el // 60:02d}:{el % 60:02d} · {apps} · Haiku", RED
        extra = " · ⟳ elaborazione" if st.get("processing") else ""
        return f"○ in ascolto, nessuna riunione{extra}", DIMC

    def _work(self):
        from .live import Cleaner, LiveTranscript
        cleaning = threading.Event()
        last_speech, last_status = time.time(), 0.0
        while not self.stop.wait(0.2):
            st = self.con._state()
            if time.time() - last_status > 1:
                self.q.put(("status", self._status(st)))
                last_status = time.time()
            if not (st and st["recording"]):
                if self.draft:
                    self.q.put(("draft", ""))
                continue
            mdir = Path(st["meeting_dir"])
            if not self.lt or self.lt.mdir != mdir:
                asr = self.lt.asr if self.lt else None
                self.lt = LiveTranscript(mdir, self.con.s)
                self.lt.asr = asr
                self.shown, self.fixed_shown = set(), set()
                self.q.put(("answer", f"— nuova registrazione {mdir.name} —"))
            lt = self.lt
            try:
                with self.con.lock:
                    _, draft, trailing = lt.step()
            except Exception as e:  # noqa: BLE001
                self.q.put(("answer", f"trascrizione live: {e}"))
                self.stop.wait(5)
                continue
            if trailing < 0.3:
                last_speech = time.time()
            from .pipeline import Line
            lines, fixed = lt.lines(), lt.cleaned()
            if not self.shown and not self.fixed_shown:  # all'apertura mostra solo le ultime righe
                self.shown.update(range(max(0, len(lines) - 20)))
                self.fixed_shown.update(i for i in fixed if i in self.shown)
            for i, l in enumerate(lines):
                if i not in self.shown:
                    self.shown.add(i)
                    ok = i in fixed
                    if ok:
                        self.fixed_shown.add(i)
                    self.q.put(("line", (i, Line(l.start, l.end, l.speaker, fixed.get(i, l.text)), ok)))
                elif i in fixed and i not in self.fixed_shown:
                    self.fixed_shown.add(i)
                    self.q.put(("fix", (i, fixed[i])))
            todo = [i for i in range(len(lines)) if i not in fixed]
            self.q.put(("draft", draft.strip()))
            if lt.driving and not cleaning.is_set() and Cleaner(lt).due(last_speech):
                cleaning.set()

                def clean(lt=lt):
                    try:
                        Cleaner(lt).run()
                    finally:
                        cleaning.clear()
                threading.Thread(target=clean, daemon=True).start()

    def close(self):
        self.stop.set()
        self.root.destroy()

    def run(self):
        threading.Thread(target=self._work, daemon=True).start()
        self.root.after(120, self._pump)
        self.root.mainloop()


def run():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise SystemExit("Manca Tkinter: sudo apt install python3-tk")
    Captions().run()


if __name__ == "__main__":
    run()
