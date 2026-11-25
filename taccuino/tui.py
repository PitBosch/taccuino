"""Interfaccia in stile Claude Code (prompt_toolkit): output che scorre sopra, box di input arrotondato in basso,
barra di stato, spinner mentre Haiku pensa, comandi /slash con suggerimenti.

Riusa tutta la logica di `console.Console` (streaming live, correzione con Haiku, comandi): qui c'è solo la vista.
"""
from __future__ import annotations

import contextlib
import queue
import re
import textwrap
import threading
import time

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import AfterInput, BeforeInput
from prompt_toolkit.filters import Condition
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from .config import DATA_DIR
from .console import BOLD, DIM, Console

# (comando, argomenti, descrizione) — mostrati nel menu sotto il box quando scrivi "/"
COMMANDS = [
    ("/riassumi", "[min]", "riassumi gli ultimi N minuti (default 5)"),
    ("/trascrizione", "[min]", "trascrizione degli ultimi N minuti"),
    ("/nota", "<testo>", "appunto con timestamp nella riunione in corso"),
    ("/start", "[titolo]", "avvia una registrazione manuale"),
    ("/stop", "", "ferma la registrazione"),
    ("/live", "", "mostra/nasconde la trascrizione live"),
    ("/stato", "", "stato del daemon"),
    ("/help", "", "elenco dei comandi"),
    ("/esci", "", "chiudi taccuino"),
]

HELP = """Comandi (anche senza slash: r, t, n, s, h, q)
  /riassumi [min]     riassume gli ultimi N minuti (default 5)        r 10
  /trascrizione [min] trascrizione degli ultimi N minuti              t 2
  /nota <testo>       appunto con timestamp                           n budget da rivedere
  /start [titolo]     registrazione manuale · /stop la ferma
  /live               mostra/nasconde la trascrizione live
  /stato · /help · /esci
Qualsiasi altra frase è una domanda a Claude Haiku sulla riunione (in corso o l'ultima).
Tab completa i comandi · ↑↓ cronologia · Esc annulla la risposta in arrivo · Ctrl-D esce"""

SPIN = "·✢✳✶✻✽✻✶✳✢"
ORANGE = "\033[38;5;209m"
GREY = "\033[38;5;245m"
RESET = "\033[0m"

STYLE = Style.from_dict({
    "border": "#888888",
    "prompt": "bold",
    "placeholder": "#6c6c6c italic",
    "footer": "#8a8a8a",
    "rec": "#ff5f5f bold",
    "model": "#8a8a8a",
    "draft": "#7a7a7a italic",
    "spinner": "#ff875f",
    "menu": "#8a8a8a",
    "menu.sel": "#d7afff bold",
    "menu.desc": "#6c6c6c",
})


class TUI(Console):
    def __init__(self):
        super().__init__()
        self.jobs: queue.Queue[str] = queue.Queue()
        self.busy: str | None = None       # etichetta dello spinner
        self.busy_since = 0.0
        self._cancel = threading.Event()
        self.menu_sel = 0
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.buf = Buffer(history=FileHistory(str(DATA_DIR / "tui_history")), multiline=False,
                          accept_handler=self._accept, on_text_changed=lambda _: setattr(self, "menu_sel", 0))
        self.app = self._build()

    # ---------- output (sopra il box) ----------
    def _emit(self, text: str):
        print(text, flush=True)  # patch_stdout lo stampa sopra l'interfaccia
        self._refresh()

    def _refresh(self):
        try:
            self.app.invalidate()
        except Exception:  # noqa: BLE001
            pass

    def out(self, text: str):
        self._emit(text)

    def info(self, msg: str):
        lines = msg.split("\n")
        self._emit("\n".join(DIM(("  ⎿  " if i == 0 else "     ") + l) for i, l in enumerate(lines)))

    def error(self, msg: str):
        self._emit(f"  ⎿  \033[31m{msg}{RESET}")

    def answer(self, text: str, secs: float):
        text = re.sub(r"\*\*(.+?)\*\*", lambda m: BOLD(m.group(1)), text.strip())
        w = max(30, self.app.output.get_size().columns - 3)
        rows = []
        for r in text.split("\n"):  # a capo con rientro, come Claude Code
            ind = re.match(r"^(\s*(?:[-*•]|\d+[.)])?\s*)", r).group(1)
            rows += textwrap.wrap(r, w, subsequent_indent=" " * len(ind)) or [""]
        body = "\n".join(("⏺ " if i == 0 else "  ") + r for i, r in enumerate(rows))
        self._emit(body + "\n" + DIM(f"  ({secs:.0f}s · Haiku)") + "\n")

    def _draw(self, permanent=(), draft=None):
        """Righe live definitive → scrollback; la bozza resta nella riga grigia sopra il box."""
        if draft is not None:
            self.draft = draft
        for p in permanent:
            self._emit("  " + p)
        self._refresh()

    @contextlib.contextmanager
    def thinking(self, label: str = "Sto pensando"):
        prev = self.busy
        self.busy, self.busy_since = label, time.time()
        self._refresh()
        try:
            yield
        finally:
            self.busy = prev
            self._refresh()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---------- comandi (in un thread: l'interfaccia resta reattiva) ----------
    def _accept(self, buf: Buffer) -> bool:
        text = buf.text.strip()
        if not text:
            return False
        self._emit(f"{GREY}> {text}{RESET}")
        v = text.split(" ", 1)[0].lower().lstrip("/")
        if v in ("q", "esci", "exit", "quit"):  # subito, anche se un altro comando è in corso
            self.app.exit()
        elif v in ("h", "aiuto", "help", "?"):
            self.info(HELP)
        elif v == "live":
            super().handle(text)
        else:
            if self.busy:
                self.info("in coda dopo il comando in corso…")
            self.jobs.put(text)
        return False  # svuota il buffer (e lo salva in cronologia)

    def _worker(self):
        while True:
            text = self.jobs.get()
            self._cancel.clear()
            self.busy, self.busy_since = "Elaboro", time.time()
            self._refresh()
            try:
                if not self.handle(text):
                    self.app.exit()
                    return
            except SystemExit as e:
                self.error(str(e))
            except Exception as e:  # noqa: BLE001
                self.error(f"errore: {e}")
            finally:
                self.busy = None
                self._refresh()

    def handle(self, line: str) -> bool:
        v = line.strip().split(" ", 1)[0].lower()
        if v in ("h", "aiuto", "help", "?", "/h", "/aiuto", "/help", "/?"):
            self.info(HELP)
            return True
        return super().handle(line)

    # ---------- vista ----------
    def _matches(self) -> list[tuple[str, str, str]]:
        t = self.buf.text
        if not t.startswith("/") or " " in t:
            return []
        return [c for c in COMMANDS if c[0].startswith(t.lower())]

    def _status(self):
        st = self._state()
        if not st:
            left = [("class:rec", " ✕ daemon spento"), ("class:footer", " · taccuino install-service")]
        elif st["recording"]:
            el = int(time.time() - st["since"])
            apps = ", ".join(st.get("apps") or []) or ("manuale" if st.get("manual") else "")
            left = [("class:rec", f" ● REC {el // 60:02d}:{el % 60:02d}"),
                    ("class:footer", f" · {apps}" if apps else "")]
        else:
            left = [("class:footer", " ○ in ascolto")]
        if st and st.get("processing"):
            q = st.get("queue") or 0
            left.append(("class:footer", f" · ⟳ elaborazione{f' (+{q})' if q else ''}"))
        if not self.show_live:
            left.append(("class:footer", " · live nascosto"))
        return left

    def _spinner(self):
        el = time.time() - self.busy_since
        g = SPIN[int(el * 8) % len(SPIN)]
        esc = " · esc per annullare" if self.busy != "Elaboro" else ""
        return ANSI(f"{ORANGE}{g} {self.busy}…{RESET} {GREY}({el:.0f}s{esc}){RESET}")

    def _draft_text(self):
        d = " ".join(self.draft.split())
        w = max(20, self.app.output.get_size().columns - 4)
        if len(d) > w:
            d = "…" + d[-(w - 1):]
        return [("class:draft", f"  {d}")]

    def _menu(self):
        out = []
        for i, (cmd, args, desc) in enumerate(self._matches()):
            sel = i == self.menu_sel
            out += [("class:menu.sel" if sel else "class:menu", f"  {cmd:<15}"),
                    ("class:menu.desc", f"{args:<10}{desc}\n")]
        return out

    def _build(self) -> Application:
        kb = KeyBindings()
        menu_open = Condition(lambda: bool(self._matches()))

        @kb.add("tab")
        def _(e):
            m = self._matches()
            if m:
                cmd = m[min(self.menu_sel, len(m) - 1)][0]
                self.buf.document = Document(cmd + " ", len(cmd) + 1)

        @kb.add("down", filter=menu_open)
        def _(e):
            self.menu_sel = (self.menu_sel + 1) % len(self._matches())

        @kb.add("up", filter=menu_open)
        def _(e):
            self.menu_sel = (self.menu_sel - 1) % len(self._matches())

        @kb.add("enter", filter=menu_open)
        def _(e):
            m = self._matches()
            cmd = m[min(self.menu_sel, len(m) - 1)][0]
            if self.buf.text.lower() != cmd:
                self.buf.document = Document(cmd, len(cmd))
            self.buf.validate_and_handle()

        @kb.add("escape", eager=True)
        def _(e):
            if self.busy and self.busy != "Elaboro":
                self._cancel.set()
                self.busy = None
                self.info("annullato")
            elif self.buf.text:
                self.buf.reset()

        @kb.add("c-c")
        def _(e):
            if self.buf.text:
                self.buf.reset()
            else:
                self.info("Ctrl-D o /esci per uscire")

        @kb.add("c-d")
        def _(e):
            if not self.buf.text:
                e.app.exit()

        def hint():
            if self.buf.text:
                return []
            return [("class:placeholder", "r riassumi · t trascrizione · n nota · /help  —  o fai una domanda")]

        border = "class:border"
        box = HSplit([
            VSplit([Window(width=1, char="╭", style=border), Window(char="─", style=border, height=1),
                    Window(width=1, char="╮", style=border)], height=1),
            VSplit([
                Window(FormattedTextControl([(border, "│ ")]), width=2),
                Window(BufferControl(self.buf, input_processors=[
                    BeforeInput([("class:prompt", "> ")]),
                    AfterInput(lambda: hint())]),  # suggerimento grigio quando il box è vuoto
                    height=1, wrap_lines=False),
                Window(width=2, content=FormattedTextControl([(border, " │")])),
            ], height=1),
            VSplit([Window(width=1, char="╰", style=border), Window(char="─", style=border, height=1),
                    Window(width=1, char="╯", style=border)], height=1),
        ])
        root = HSplit([
            ConditionalContainer(Window(FormattedTextControl(self._draft_text), height=1),
                                 filter=Condition(lambda: bool(self.draft.strip()) and self.show_live)),
            ConditionalContainer(Window(FormattedTextControl(self._spinner), height=1),
                                 filter=Condition(lambda: bool(self.busy))),
            box,
            ConditionalContainer(Window(FormattedTextControl(self._menu), dont_extend_height=True),
                                 filter=menu_open),
            VSplit([Window(FormattedTextControl(self._status), height=1),
                    Window(FormattedTextControl([("class:model", "Haiku ")]), height=1,
                           dont_extend_width=True)]),
        ])
        return Application(layout=Layout(root, focused_element=self.buf), key_bindings=kb, style=STYLE,
                           full_screen=False, mouse_support=False, refresh_interval=0.25, erase_when_done=True)

    def run(self):
        print(BOLD("✻ taccuino") + DIM("  note di riunione locali · /help per i comandi"))
        st = self._state()
        if not st:
            print(DIM("  ⎿  ") + "\033[31mil daemon non è attivo:\033[0m taccuino install-service")
        mdir, st = self.meeting()
        if st and st["recording"] and mdir:
            behind = (time.time() - st["since"]) - self._lt(mdir).cursor
            if behind > 60:
                print(DIM(f"  ⎿  recupero {behind / 60:.0f} min di audio già registrato…"))
        print()
        threading.Thread(target=self._bg, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        try:
            with patch_stdout(raw=True):
                self.app.run()
        finally:
            self.stop_ev.set()


def run():
    TUI().run()
