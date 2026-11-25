"""Console interattiva: `taccuino` senza argomenti.

  r [min]        riassumi gli ultimi N minuti (default 5)
  t [min]        mostra la trascrizione degli ultimi N minuti (default 5)
  n testo        appunto: Haiku (il segretario) lo interpreta, lo mette nella nota e prepara task Jira
                 ("n crea task: …" lo crea subito sul tuo Jira; altrimenti lo propone)
  jira           proposte Jira · jira ok N | jira ok tutte · jira no N
  start [titolo] avvia una registrazione manuale · stop  la ferma
  live           mostra/nasconde le righe trascritte mentre arrivano
  s              stato · h  aiuto · q  esci
  qualsiasi altra frase → domanda a Claude sulla riunione (in corso o l'ultima)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from .config import DATA_DIR, MEETINGS_DIR, load_settings
from .live import LiveTranscript
from .pipeline import Line, fmt_ts, user_notes_of

TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


DIM, BOLD, RED, CYAN, YEL = (lambda s: _c("2", s)), (lambda s: _c("1", s)), (lambda s: _c("31", s)), \
    (lambda s: _c("36", s)), (lambda s: _c("33", s))


def _fmt(l: Line) -> str:
    who = CYAN(l.speaker) if l.speaker != "Io" else YEL(l.speaker)
    return f"{DIM(fmt_ts(l.start))} {who}: {l.text}"


# comandi accettati anche con lo slash (/riassumi, /nota …)
SLASH_ALIASES = {"riassumi", "r", "riassunto", "trascrizione", "t", "transcript", "nota", "n", "start", "stop",
                 "live", "stato", "s", "status", "help", "aiuto", "h", "esci", "q", "exit", "quit", "jira"}


class Console:
    def __init__(self):
        from .cli import _state
        self._state = _state
        self.s = load_settings()
        self.lock = threading.Lock()
        self.live: LiveTranscript | None = None
        self.show_live = True
        self.stop_ev = threading.Event()

    # ---------- quale riunione ----------
    def meeting(self) -> tuple[Path | None, dict | None]:
        st = self._state()
        if st and st["recording"]:
            return Path(st["meeting_dir"]), st
        dirs = sorted(p.parent for p in MEETINGS_DIR.glob("*/meta.json"))
        return (dirs[-1] if dirs else None), st

    def _lt(self, mdir: Path) -> LiveTranscript:
        if not self.live or self.live.mdir != mdir:
            asr = self.live.asr if self.live else None  # il modello resta caricato tra una riunione e l'altra
            self.live = LiveTranscript(mdir, self.s)
            self.live.asr = asr
        return self.live

    def lines(self, final_ok=True) -> tuple[list[Line], Path | None, bool]:
        """Righe della riunione corrente (aggiornate) o dell'ultima riunione."""
        mdir, st = self.meeting()
        if not mdir:
            return [], None, False
        recording = bool(st and st["recording"])
        if not recording and final_ok and (mdir / "transcript.json").exists():
            return [Line(**d) for d in json.loads((mdir / "transcript.json").read_text())], mdir, False
        lt = self._lt(mdir)
        with self.lock:
            if lt.asr is None:
                self.info("carico il modello di trascrizione …")
            lt.update(final=not recording)
        return lt.clean_lines(), mdir, recording

    # ---------- output ----------
    # Layout mentre aspetti un comando:   [righe corrette …] / [bozza grigia che si aggiorna] / [prompt]
    waiting = False
    draft = ""
    _drawn = False
    _backlog: list[str] = []
    _draw_lock = threading.Lock()

    def _draw(self, permanent: list[str] = (), draft: str | None = None):
        with self._draw_lock:
            if draft is not None:
                self.draft = draft
            if not self.waiting or not TTY:
                self._backlog += list(permanent)
                return
            try:
                import readline
                buf = readline.get_line_buffer()
            except Exception:  # noqa: BLE001
                buf = ""
            w = max(20, shutil.get_terminal_size().columns - 1)
            out = ("\033[1A" if self._drawn else "") + "\r\033[J"
            for p in permanent:
                out += p + "\n"
            d = " ".join(self.draft.split()) if self.show_live else ""
            if d:
                out += DIM("… " + (d if len(d) <= w - 2 else "…" + d[-(w - 3):])) + "\n"
            self._drawn = bool(d)
            sys.stdout.write(out + self.prompt() + buf)
            sys.stdout.flush()

    def say(self, msg: str):
        """Stampa sopra il prompt senza rovinare quello che l'utente sta scrivendo."""
        self._draw([msg])

    # Hook di output: la console semplice stampa, la TUI (tui.py) li ridefinisce.
    def out(self, text: str):
        print(text, flush=True)

    def info(self, msg: str):
        print(DIM(msg), flush=True)

    def error(self, msg: str):
        print(RED(msg), flush=True)

    def answer(self, text: str, secs: float):
        if TTY:
            text = re.sub(r"\*\*(.+?)\*\*", lambda m: BOLD(m.group(1)), text)
        print(text + "\n" + DIM(f"({secs:.0f}s)"), flush=True)

    @contextlib.contextmanager
    def thinking(self, label: str = "Claude sta pensando"):
        print(DIM(f"… {label}"), flush=True)
        try:
            yield
        finally:
            print("\033[1A\033[K" if TTY else "", end="", flush=True)

    def cancelled(self) -> bool:
        """La TUI lo ridefinisce (Esc annulla la risposta in arrivo)."""
        return False

    def prompt(self) -> str:
        st = self._state()
        if not st:
            head = RED("✕ daemon spento")
        elif st["recording"]:
            el = int(time.time() - st["since"])
            head = RED("●") + f" {el // 60:02d}:{el % 60:02d}"
        else:
            head = DIM("○")
        if st and st.get("processing"):
            head += DIM(" ⟳")
        return f"{head} › "

    # ---------- trascrizione in streaming (solo mentre la console è aperta) ----------
    def _bg(self):
        """Mostra le righe live. Di norma trascrive il live worker del daemon e qui si legge soltanto;
        se il worker non c'è (live_transcribe spento), trascrive la console stessa."""
        from .live import Cleaner
        cleaning = threading.Event()
        last_speech = time.time()
        shown: set[int] = set()
        cur_dir = None
        while not self.stop_ev.wait(0.2):
            mdir, st = self.meeting()
            if not (st and st["recording"] and mdir):
                if self.draft:
                    self._draw(draft="")
                continue
            lt = self._lt(mdir)
            first = cur_dir != mdir
            if first:
                shown, cur_dir = set(), mdir
            try:
                if lt.driver_alive():
                    draft, trailing = lt.shared_draft(), 1.0
                else:
                    with self.lock:
                        if lt.asr is None:
                            self.say(DIM("carico il modello di trascrizione …"))
                            lt.load()
                            if time.time() - st["since"] - lt.cursor > 60:
                                self.say(DIM("recupero l'audio già registrato …"))
                                lt.update()
                        _, draft, trailing = lt.step()
            except Exception as e:  # noqa: BLE001
                self.say(RED(f"trascrizione live: {e}"))
                self.stop_ev.wait(5)
                continue
            if trailing < 0.3:
                last_speech = time.time()
            lines, fixed = lt.lines(), lt.cleaned()
            new = [i for i in sorted(fixed) if i not in shown and i < len(lines)]
            if first:  # all'apertura solo le ultime righe
                shown.update(new[:-10])
                new = new[-10:]
            shown.update(new)
            out = [_fmt(Line(lines[i].start, lines[i].end, lines[i].speaker, fixed[i])) for i in new if fixed[i]]
            raw = " ".join(lines[i].text for i in range(len(lines)) if i not in fixed)
            self._draw(out if self.show_live else [], draft=(raw + " " + draft).strip())
            if lt.driving and not cleaning.is_set():
                todo = Cleaner(lt).pending()
                if todo and (time.time() - last_speech > 1.2 or len(todo) >= 6):
                    cleaning.set()

                    def work(lt=lt):
                        try:
                            Cleaner(lt).run()
                        finally:
                            cleaning.clear()
                    threading.Thread(target=work, daemon=True).start()

    # ---------- comandi ----------
    def window(self, lines: list[Line], minutes: float) -> list[Line]:
        if not lines:
            return []
        end = max(l.end for l in lines)
        return [l for l in lines if l.end >= end - minutes * 60]

    def claude(self, question: str, lines: list[Line], scope: str, mdir: Path):
        from .summarize import ask
        if not lines:
            self.info("niente da riassumere: nessun parlato trascritto.")
            return
        notes = user_notes_of(mdir)
        text = "\n".join(f"[{fmt_ts(l.start)}] {l.speaker}: {l.text}" for l in lines)
        if notes:
            text = f"APPUNTI DELL'UTENTE:\n{notes}\n\n{text}"
        t = time.time()
        try:
            with self.thinking():
                out = ask(self.s, text, question, scope)
        except Exception as e:  # noqa: BLE001
            self.error(f"Claude non disponibile: {e}")
            return
        if self.cancelled():
            return
        self.answer(out, time.time() - t)

    def handle(self, line: str) -> bool:
        from .cli import _send
        from .summarize import RECAP
        verb, _, arg = line.strip().partition(" ")
        v = verb.lower()
        if v.startswith("/"):
            v = v[1:]
            if v not in SLASH_ALIASES:
                self.error(f"comando sconosciuto: /{v}  (/help per l'elenco)")
                return True
        if v in ("q", "esci", "exit", "quit"):
            return False
        if v in ("h", "aiuto", "help", "?"):
            self.info(__doc__.split("\n", 2)[2].rstrip())
        elif v in ("s", "stato", "status"):
            from .cli import main
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["status"])
            self.info(buf.getvalue().rstrip())
        elif v in ("r", "riassumi", "riassunto"):
            mins = float(arg) if arg.strip() else 5
            lines, mdir, rec = self.lines()
            scope = f"degli ultimi {mins:g} minuti della riunione{' in corso' if rec else ''}"
            self.claude(RECAP, self.window(lines, mins), scope, mdir)
        elif v in ("t", "trascrizione", "transcript"):
            mins = float(arg) if arg.strip() else 5
            lines, _, _ = self.lines()
            rows = [_fmt(l) for l in self.window(lines, mins)]
            self.out("\n".join(rows)) if rows else self.info("(nessun parlato)")
        elif v in ("n", "nota"):
            if not arg.strip():
                self.error("scrivi il testo dell'appunto: n <testo>")
                return True
            st = self._state()
            mdir, el = None, None
            if st and st["recording"]:
                mdir, el = Path(st["meeting_dir"]), time.time() - st["since"]
                with open(mdir / "notes.txt", "a") as f:
                    f.write(f"- [{fmt_ts(el)}] {arg.strip()}\n")
            self.info("appunto salvato, lo passo al segretario …" if mdir else
                      "nessuna riunione in corso: lo uso solo per Jira …")
            text = arg.strip()

            def secretary(text=text, mdir=mdir, el=el):
                from .secretary import handle_note
                t = time.time()
                self.answer("\n".join(handle_note(self.s, text, mdir, el)), time.time() - t)
            threading.Thread(target=secretary, daemon=True).start()
        elif v == "jira":
            from .secretary import jira_command
            mdir, _ = self.meeting()
            t = time.time()
            with self.thinking("Jira"):
                msgs = jira_command(self.s, mdir, arg)
            self.answer("\n".join(msgs), time.time() - t)
        elif v == "start":
            _send(f"start {arg}".strip()); self.info("● registrazione avviata")
        elif v == "stop":
            _send("stop"); self.info("■ registrazione fermata: transcript completo e note tra qualche minuto")
        elif v == "live":
            self.show_live = not self.show_live
            self.info(f"righe live {'visibili' if self.show_live else 'nascoste'}")
        elif line.strip():
            self.question(line.strip())
        return True

    def question(self, q: str):
        lines, mdir, rec = self.lines()
        self.claude(q, lines, "della riunione in corso finora" if rec else "dell'ultima riunione", mdir)

    def run(self):
        try:
            import readline
            hist = DATA_DIR / "console_history"
            try:
                readline.read_history_file(hist)
            except (FileNotFoundError, OSError):
                pass
            def ready():
                self.waiting = True
            readline.set_pre_input_hook(ready)
        except ImportError:
            readline, hist = None, None
        st = self._state()
        print(BOLD("taccuino") + DIM("  r = riassumi 5 min · t = trascrizione · n = nota · h = aiuto · q = esci"))
        if not st:
            print(RED("il daemon non è attivo: ") + "taccuino install-service")
        mdir, st = self.meeting()
        if st and st["recording"] and mdir:
            behind = (time.time() - st["since"]) - self._lt(mdir).cursor
            if behind > 60:
                print(DIM(f"recupero {behind / 60:.0f} min di audio già registrato…"))
        threading.Thread(target=self._bg, daemon=True).start()
        try:
            while True:
                with self._draw_lock:
                    for p in self._backlog:
                        print(p)
                    self._backlog.clear()
                    self._drawn = False
                    self.waiting = not readline  # con readline lo attiva il pre-input hook, a prompt disegnato
                try:
                    line = input(self.prompt())
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue
                finally:
                    with self._draw_lock:
                        self.waiting, self._drawn = False, False
                try:
                    if not self.handle(line):
                        break
                except KeyboardInterrupt:
                    print(DIM("annullato"))
                except SystemExit as e:
                    print(RED(str(e)))
                except Exception as e:  # noqa: BLE001
                    print(RED(f"errore: {e}"))
        finally:
            self.stop_ev.set()
            if readline and hist:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                readline.set_history_length(500)
                readline.write_history_file(hist)


def run():
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    Console().run()
