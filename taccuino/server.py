"""Server locale (FastAPI) + UI web su http://127.0.0.1:8765."""
from __future__ import annotations

import asyncio
import json
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from . import notes as notes_mod
from .audio import Recorder, Segment, load_file, segment_array
from .config import DATA_DIR, load_settings
from .engines import ASR, LLM
from .store import Store

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self):
        self.settings = load_settings()
        self.profile = self.settings.resolved_profile()
        self.store = Store()
        self._asr: ASR | None = None
        self._llm: LLM | None = None
        self._load_lock = threading.Lock()
        self.recorders: dict[int, Recorder] = {}
        self.subs: dict[int, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self.jobs: queue.Queue = queue.Queue()
        self.busy: dict[int, str] = {}
        threading.Thread(target=self._asr_worker, daemon=True).start()

    # --- modelli caricati alla prima necessità ---
    @property
    def asr(self) -> ASR:
        with self._load_lock:
            if self._asr is None:
                self._asr = ASR(self.profile, self.settings.language)
            return self._asr

    @property
    def llm(self) -> LLM:
        with self._load_lock:
            if self._llm is None:
                self._llm = LLM(self.profile)
            return self._llm

    # --- eventi verso il browser (SSE) ---
    def emit(self, mid: int, kind: str, **data):
        for loop, q in list(self.subs.get(mid, [])):
            loop.call_soon_threadsafe(q.put_nowait, {"type": kind, **data})

    # --- trascrizione ---
    def _asr_worker(self):
        while True:
            mid, seg = self.jobs.get()
            try:
                text, _ = self.asr.transcribe(seg.audio)
                if text:
                    self.store.add_segment(mid, seg.speaker, seg.t0, text)
                    self.emit(mid, "segment", speaker=seg.speaker, t0=seg.t0, text=text)
            except Exception as e:  # noqa: BLE001
                self.emit(mid, "error", message=f"ASR: {e}")
            finally:
                self.emit(mid, "backlog", n=self.jobs.qsize())

    def start(self, mid: int):
        if mid in self.recorders:
            return
        self.asr  # carica il modello prima di registrare
        sysrc = self.settings.system_source if self.settings.capture_system_audio else None
        rec_dir = DATA_DIR / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        # offset temporale se si riprende una riunione già registrata
        segs = self.store.segments(mid)
        offset = (segs[-1]["t0"] + 30) if segs else 0.0

        def on_seg(s: Segment):
            s.t0 += offset
            self.jobs.put((mid, s))

        rec = Recorder(self.settings.mic_source, sysrc, on_seg,
                       wav_path=str(rec_dir / f"{mid}-{int(time.time())}.s16le"))
        rec.start()
        self.recorders[mid] = rec
        threading.Thread(target=self._levels, args=(mid, rec), daemon=True).start()

    def _levels(self, mid, rec: Recorder):
        while self.recorders.get(mid) is rec:
            self.emit(mid, "level", me=rec.level[0], them=rec.level[1])
            time.sleep(0.25)

    def stop(self, mid: int):
        rec = self.recorders.pop(mid, None)
        if rec:
            err = rec.stop()
            self.store.update(mid, ended=time.time())
            if err.strip():
                self.emit(mid, "error", message=err.strip()[-300:])


app_state: App | None = None
api = FastAPI(title="Taccuino")


def S() -> App:
    global app_state
    if app_state is None:
        app_state = App()
    return app_state


@api.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@api.get("/api/status")
def status():
    s = S()
    return {"profile": s.settings.profile, "asr": f"{s.profile.asr_model} @ {s.profile.asr_device}",
            "llm": f"{s.profile.llm_model} @ {s.profile.llm_device}", "recording": list(s.recorders),
            "asr_loaded": s._asr is not None, "llm_loaded": s._llm is not None, "backlog": s.jobs.qsize()}


@api.get("/api/meetings")
def meetings():
    return S().store.list()


@api.post("/api/meetings")
async def new_meeting(req: Request):
    body = await req.json() if (await req.body()) else {}
    mid = S().store.create(body.get("title", ""))
    return S().store.get(mid)


@api.get("/api/meetings/{mid}")
def get_meeting(mid: int):
    m = S().store.get(mid)
    if not m:
        raise HTTPException(404)
    m["recording"] = mid in S().recorders
    m["busy"] = S().busy.get(mid)
    return m


@api.patch("/api/meetings/{mid}")
async def patch_meeting(mid: int, req: Request):
    S().store.update(mid, **(await req.json()))
    return {"ok": True}


@api.delete("/api/meetings/{mid}")
def delete_meeting(mid: int):
    S().stop(mid)
    S().store.delete(mid)
    return {"ok": True}


@api.post("/api/meetings/{mid}/start")
async def start(mid: int):
    try:
        await asyncio.to_thread(S().start, mid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
    return {"ok": True}


@api.post("/api/meetings/{mid}/stop")
async def stop(mid: int):
    await asyncio.to_thread(S().stop, mid)
    return {"ok": True}


def _bg(mid: int, label: str, fn):
    s = S()
    if s.busy.get(mid):
        raise HTTPException(409, "già in corso")
    s.busy[mid] = label

    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            s.emit(mid, "error", message=str(e))
        finally:
            s.busy.pop(mid, None)
            s.emit(mid, "done", job=label)

    threading.Thread(target=run, daemon=True).start()


@api.post("/api/meetings/{mid}/enhance")
def enhance(mid: int):
    s = S()

    def job():
        # aspetta che la coda di trascrizione sia vuota
        while s.jobs.qsize():
            s.emit(mid, "status", message=f"Attendo trascrizione ({s.jobs.qsize()} segmenti)…")
            time.sleep(1)
        m = s.store.get(mid)
        s.emit(mid, "status", message="Carico il modello…")
        llm = s.llm
        s.emit(mid, "enhance_start")
        out = notes_mod.enhance(llm, m["title"], m["notes"], m["segments"], s.settings.language,
                                on_token=lambda t: s.emit(mid, "token", text=t),
                                on_status=lambda msg: s.emit(mid, "status", message=msg))
        s.store.update(mid, enhanced=out)
        if not m["title"]:
            t = llm.chat("Rispondi solo con un titolo di massimo 6 parole, senza virgolette.",
                         "Titolo per queste note:\n" + out[:2000], max_new_tokens=24)
            s.store.update(mid, title=t.strip().strip('"#* '))
            s.emit(mid, "title", title=t.strip().strip('"#* '))
        s.emit(mid, "enhanced", text=out)

    _bg(mid, "enhance", job)
    return {"ok": True}


@api.post("/api/meetings/{mid}/ask")
async def ask(mid: int, req: Request):
    q = (await req.json())["q"]
    s = S()

    def job():
        m = s.store.get(mid)
        s.emit(mid, "answer_start", q=q)
        out = notes_mod.ask(s.llm, q, m["notes"], m["segments"], on_token=lambda t: s.emit(mid, "answer_token", text=t))
        s.emit(mid, "answer", text=out)

    _bg(mid, "ask", job)
    return {"ok": True}


@api.post("/api/import")
async def import_file(file: UploadFile):
    """Importa un file audio/video esistente e lo trascrive."""
    s = S()
    tmp = Path(tempfile.mkdtemp()) / (file.filename or "audio")
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    mid = s.store.create(Path(file.filename or "").stem)

    def job():
        s.emit(mid, "status", message="Decodifico l'audio…")
        x = load_file(str(tmp))
        segs = segment_array(x)
        for i, seg in enumerate(segs, 1):
            s.emit(mid, "status", message=f"Trascrivo {i}/{len(segs)}…")
            text, _ = s.asr.transcribe(seg.audio)
            if text:
                s.store.add_segment(mid, seg.speaker, seg.t0, text)
                s.emit(mid, "segment", speaker=seg.speaker, t0=seg.t0, text=text)
        s.store.update(mid, ended=time.time())
        shutil.rmtree(tmp.parent, ignore_errors=True)

    _bg(mid, "import", job)
    return {"id": mid}


@api.get("/api/meetings/{mid}/events")
async def events(mid: int, req: Request):
    s = S()
    q: asyncio.Queue = asyncio.Queue()
    entry = (asyncio.get_running_loop(), q)
    s.subs.setdefault(mid, []).append(entry)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while not await req.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            s.subs[mid].remove(entry)

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/api/meetings/{mid}/export.md")
def export_md(mid: int):
    m = S().store.get(mid)
    body = f"# {m['title'] or 'Riunione'}\n\n{m['enhanced'] or m['notes']}\n\n---\n## Transcript\n\n"
    body += "\n".join(notes_mod.format_transcript(m["segments"]))
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(body, media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="riunione-{mid}.md"'})
