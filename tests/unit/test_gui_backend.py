"""In-process tests for the GUI's stdio JSON-lines backend."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import ProviderConfig, TaskName
from corpusqa.gui_backend.models import ModelInfo, ModelListing
from corpusqa.gui_backend.server import Server
from corpusqa.ingest import converter, pipeline
from tests.unit.fakes import FakeClient

REPO = Path(__file__).resolve().parents[2]
QUOTE = "Community gardens"

Msg = dict[str, Any]


def _config_text(
    *,
    extract_provider: str = "local",
    extract_model: str = "ollama/m",
    extract_context: int = 32768,
    extract_price: float = 0.0,
    confirm_above: float = 100.0,
    extra_providers: str = "",
    route_provider: str = "local",
    route_model: str = "ollama/m",
    sweep_threshold: int = 150,
) -> str:
    def task(name: str, provider: str, model: str, ctx: int, price: float) -> str:
        return (
            f"  {name}:\n"
            f'    model: "{model}"\n'
            f"    provider: {provider}\n"
            f"    temperature: 0.2  # keep this comment\n"
            f"    context_window: {ctx}\n"
            f"    cost_per_mtok_in: {price}\n"
            f"    cost_per_mtok_out: {price}\n"
        )

    tasks = (
        "".join(
            task(n, route_provider, route_model, 32768, 0.0)
            for n in ("catalog_summarize", "query_route")
        )
        + task("synthesize", "local", "ollama/m", 32768, 0.0)
    ) + task("extract", extract_provider, extract_model, extract_context, extract_price)
    return (
        "providers:\n"
        "  local:\n"
        '    api_base: "http://localhost:11434"\n'
        f"{extra_providers}"
        "tasks:\n"
        f"{tasks}"
        "budget:\n"
        f"  confirm_above_usd: {confirm_above}\n"
        "query:\n"
        f"  sweep_threshold: {sweep_threshold}\n"
    )


def _write_config(tmp_path: Path, **kwargs: Any) -> Path:
    path = tmp_path / "cfg" / "corpusqa.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_text(**kwargs), encoding="utf-8")
    return path


def _extraction(relevant: bool = True) -> str:
    return json.dumps(
        {
            "file_hash": "ignored",
            "relevant": relevant,
            "confidence": 0.9,
            "findings": [
                {
                    "claim": "Gardens are proposed",
                    "quote": QUOTE,
                    "heading_path": [],
                    "page_label": None,
                }
            ]
            if relevant
            else [],
        }
    )


@pytest.fixture()
def fake_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces Docling with a passthrough so runs stay fast."""
    monkeypatch.setattr(pipeline.converter, "check_converter_available", lambda: None)

    def convert(
        source: Path,
        file_hash: str,
        cache_dir: Path,
        ocr: str,
        pdf_backend: str = "docling_parse",
        document_converter: object | None = None,
    ) -> converter.ConversionResult:
        cache_dir.mkdir(parents=True, exist_ok=True)
        md_path = cache_dir / f"{file_hash}.md"
        md_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        (cache_dir / f"{file_hash}.pages.json").write_text(
            json.dumps([[0, "1"]]), encoding="utf-8"
        )
        (cache_dir / f"{file_hash}.meta.json").write_text(
            json.dumps({"converter_version": converter.CONVERTER_VERSION}),
            encoding="utf-8",
        )
        return converter.ConversionResult(
            file_hash=file_hash,
            md_cache_path=md_path,
            parse_status=converter.STATUS_OK,
            parse_error=None,
            page_count=None,
        )

    monkeypatch.setattr(pipeline.converter, "convert", convert)
    monkeypatch.setattr(pipeline.converter, "build_converter", lambda *a: None)


@pytest.fixture()
def md_corpus(tmp_path: Path, fake_convert: None) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "gardens.md").write_text(
        "# Garden\n\n## Ideas\nCommunity gardens grow food.\n", encoding="utf-8"
    )
    return root


class Harness:
    """Runs a Server on a thread with queue-backed stdin and captured stdout."""

    def __init__(self, server_factory: Callable[[Harness], Server]) -> None:
        self._in: queue.Queue[str] = queue.Queue()
        self._cond = threading.Condition()
        self._buf = ""
        self.messages: list[Msg] = []
        self.server = server_factory(self)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()), daemon=True
        )
        self._thread.start()

    # stdin side
    def readline(self) -> str:
        return self._in.get()

    def send(self, msg: Msg) -> None:
        self._in.put(json.dumps(msg) + "\n")

    # stdout side
    def write(self, text: str) -> int:
        with self._cond:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self.messages.append(json.loads(line))  # every line is JSON
            self._cond.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def wait_for(self, pred: Callable[[Msg], bool], timeout: float = 30.0) -> Msg:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self.messages:
                    if pred(m):
                        return m
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out; got {self.messages}")
                self._cond.wait(left)

    def reply(self, req_id: str) -> Msg:
        return self.wait_for(lambda m: m.get("id") == req_id)

    def event(self, name: str, timeout: float = 30.0) -> Msg:
        return self.wait_for(lambda m: m.get("event") == name, timeout)

    def events(self) -> list[Msg]:
        with self._cond:
            return [m for m in self.messages if "event" in m]

    def clear(self) -> None:
        """Forgets the messages so far (e.g. between two runs)."""
        with self._cond:
            self.messages.clear()

    def close(self) -> None:
        self._in.put("")
        self._thread.join(timeout=30)
        assert not self._thread.is_alive()


@pytest.fixture()
def harness() -> Iterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(
        config_path: Path,
        client: object | None = None,
        list_models_fn: Callable[..., Any] | None = None,
    ) -> Harness:
        def factory(h: Harness) -> Server:
            kwargs: dict[str, Any] = {}
            if client is not None:
                kwargs["client_factory"] = lambda _config: client
            if list_models_fn is not None:
                kwargs["list_models_fn"] = list_models_fn
            return Server(config_path, h, h, **kwargs)

        h = Harness(factory)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def _start(folder: Path, **params: Any) -> Msg:
    return {
        "id": "run",
        "cmd": "run.start",
        "params": {
            "folder": str(folder),
            "question": "what ideas exist?",
            "relevance": "recall",
            "mode": "sweep",
            "synthesize": False,
            "use_cache": False,
            **params,
        },
    }


def test_run_streams_progress_results_and_done(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(md_corpus))
    assert h.reply("run")["result"] == {"started": True}
    done = h.event("done")
    kinds = [e["event"] for e in h.events()]
    assert kinds[-1] == "done"
    phases = {e["phase"] for e in h.events() if e["event"] == "progress"}
    assert phases == {"converting", "reading"}
    fr = h.event("file_result")
    assert fr["rel_path"] == "gardens.md" and fr["relevant"] is True
    finding = fr["findings"][0]
    assert finding["start"] is not None and finding["verified"] is True
    assert finding["quote"] == QUOTE and finding["page_label"] == "1"
    assert done.pop("results_path").endswith("-what-ideas-exist.json")
    assert done == {
        "event": "done",
        "failures": [],
        "answer": None,
        "citations": [],
        "cancelled": False,
    }


def test_run_emits_conversion_flags(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "broken.docx").write_bytes(b"this is not a zip archive")
    (root / "gardens.md").write_text(
        "# Garden\n\nCommunity gardens grow food.\n", encoding="utf-8"
    )
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(root))
    done = h.event("done", timeout=120)
    flags = h.event("flags")
    assert [i["rel_path"] for i in flags["items"]] == ["broken.docx"]
    assert flags["items"][0]["status"] == converter.STATUS_PARSE_FAILED
    assert flags["items"][0]["error"]
    assert done["cancelled"] is False


def test_run_with_missing_key_emits_error_before_work(
    md_corpus: Path,
    tmp_path: Path,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cfg = _write_config(
        tmp_path,
        extract_provider="remote",
        extract_model="openai/x",
        extra_providers=(
            "  remote:\n"
            '    api_base: "https://example.invalid/v1"\n'
            "    api_key_env: MISSING_KEY\n"
        ),
    )
    h = harness(cfg, client=FakeClient({}))
    h.send(_start(md_corpus))
    h.event("done")
    events = h.events()
    assert events[0]["event"] == "error"
    assert "MISSING_KEY" in events[0]["message"] and ".env" in events[0]["message"]
    assert not any(e["event"] == "progress" for e in events)
    assert not (md_corpus / ".corpusqa").exists()


def test_dotenv_next_to_config_supplies_key(
    md_corpus: Path,
    tmp_path: Path,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CQA_DOTENV_KEY", raising=False)
    cfg = _write_config(
        tmp_path,
        extract_provider="remote",
        extract_model="openai/x",
        extra_providers=(
            "  remote:\n"
            '    api_base: "https://example.invalid/v1"\n'
            "    api_key_env: CQA_DOTENV_KEY\n"
        ),
    )
    (cfg.parent / ".env").write_text("CQA_DOTENV_KEY=sk-test\n", encoding="utf-8")
    try:
        h = harness(cfg, client=FakeClient({TaskName.EXTRACT: [_extraction()]}))
        h.send(_start(md_corpus))
        assert not [e for e in h.events() if e["event"] == "error"]
        assert h.event("done")["cancelled"] is False
    finally:
        import os

        os.environ.pop("CQA_DOTENV_KEY", None)


class _SlowClient(FakeClient):
    async def complete(self, task: TaskName, messages: Any, **kw: Any) -> Any:
        await asyncio.sleep(0.3)
        return await super().complete(task, messages, **kw)


class _GatedClient(FakeClient):
    """Completes the first call; every later call waits until it is cancelled.

    The fake ignores ``max_concurrency``, so with a plain delay all calls can
    finish before the test's cancel arrives. Gating makes the run still be in
    progress, deterministically, when the cancel is sent.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._started = 0

    async def complete(self, task: TaskName, messages: Any, **kw: Any) -> Any:
        self._started += 1
        if self._started > 1:
            await asyncio.Event().wait()  # never set: only cancellation ends it
        return await super().complete(task, messages, **kw)


def test_cancel_stops_and_reports(
    tmp_path: Path, fake_convert: None, harness: Callable[..., Harness]
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for n in range(6):
        (root / f"f{n}.md").write_text(
            f"# F{n}\n\nCommunity gardens {n}.\n", encoding="utf-8"
        )
    cfg = tmp_path / "cfg" / "corpusqa.yaml"
    cfg.parent.mkdir()
    # one extraction at a time so the run is still going after the first
    cfg.write_text(
        _config_text().replace(
            '    api_base: "http://localhost:11434"\n',
            '    api_base: "http://localhost:11434"\n    max_concurrency: 1\n',
        ),
        encoding="utf-8",
    )
    fake = _GatedClient({TaskName.EXTRACT: [_extraction()] * 6})
    h = harness(cfg, client=fake)
    h.send(_start(root))
    h.event("file_result")  # the query is under way
    h.send({"id": "c", "cmd": "run.cancel"})
    assert h.reply("c")["result"] == {"cancelled": True}
    done = h.event("done")
    assert done["cancelled"] is True
    assert len(fake.calls[TaskName.EXTRACT]) < 6
    assert len([e for e in h.events() if e["event"] == "file_result"]) < 6


def test_cancel_during_conversion_uses_should_stop(
    tmp_path: Path,
    fake_convert: None,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for n in range(5):
        (root / f"f{n}.md").write_text(f"# F{n}\n\ntext {n}\n", encoding="utf-8")
    real = pipeline.converter.convert

    def slow_convert(*a: Any, **kw: Any) -> converter.ConversionResult:
        time.sleep(0.3)
        return real(*a, **kw)

    monkeypatch.setattr(pipeline.converter, "convert", slow_convert)
    fake = FakeClient({})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(root))
    h.event("progress")
    h.send({"id": "c", "cmd": "run.cancel"})
    done = h.event("done")
    assert done["cancelled"] is True
    converting = [e for e in h.events() if e["event"] == "progress"]
    assert converting[-1]["done"] < 5
    assert not fake.calls


def test_cost_gate_waits_for_reply(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    cfg = _write_config(tmp_path, extract_price=1000.0, confirm_above=0.0)
    h = harness(cfg, client=fake)
    h.send(_start(md_corpus))
    ask = h.event("cost.confirm")
    assert ask["estimate"]["total_usd"] > 0 and ask["estimate"]["lines"]
    # The loop is blocked inside confirm; other requests must still not hang.
    h.send(
        {
            "id": "r",
            "cmd": "cost.reply",
            "params": {"run_id": ask["run_id"], "proceed": False},
        }
    )
    assert h.reply("r")["result"] == {}
    done = h.event("done")
    assert done["cancelled"] is True
    assert TaskName.EXTRACT not in fake.calls


def test_cost_gate_proceeds_on_yes(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    cfg = _write_config(tmp_path, extract_price=1000.0, confirm_above=0.0)
    h = harness(cfg, client=fake)
    h.send(_start(md_corpus))
    ask = h.event("cost.confirm")
    h.send(
        {
            "id": "r",
            "cmd": "cost.reply",
            "params": {"run_id": ask["run_id"], "proceed": True},
        }
    )
    done = h.event("done")
    assert done["cancelled"] is False
    assert len(fake.calls[TaskName.EXTRACT]) == 1


_LM = '  lms:\n    kind: lm_studio\n    api_base: "http://localhost:1234/v1"\n'


def _lm_listing(listing: ModelListing) -> Callable[..., Any]:
    async def fn(name: str, provider: ProviderConfig, **_: Any) -> ModelListing:
        return listing

    return fn


def test_lm_studio_context_too_small_errors_before_work(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    cfg = _write_config(
        tmp_path,
        extract_provider="lms",
        extract_model="lm_studio/qwen",
        extract_context=16384,
        extra_providers=_LM,
    )
    listing = ModelListing(
        models=[ModelInfo(id="lm_studio/qwen", loaded=True, loaded_context=4096)]
    )
    h = harness(cfg, client=FakeClient({}), list_models_fn=_lm_listing(listing))
    h.send(_start(md_corpus))
    h.event("done")
    events = h.events()
    assert events[0]["event"] == "error"
    assert "4096" in events[0]["message"] and "16384" in events[0]["message"]
    # Names the task, since each task has its own Context window.
    assert "extract task's Context window" in events[0]["message"]
    assert not any(e["event"] == "progress" for e in events)


def _lms_cfg(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path,
        extract_provider="lms",
        extract_model="lm_studio/qwen",
        extract_context=16384,
        extra_providers=_LM,
    )


def test_lm_studio_model_not_loaded_errors_before_work(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    listing = ModelListing(models=[ModelInfo(id="lm_studio/qwen", loaded=False)])
    fake = FakeClient({})
    h = harness(_lms_cfg(tmp_path), client=fake, list_models_fn=_lm_listing(listing))
    h.send(_start(md_corpus))
    h.event("done")
    events = h.events()
    assert events[0]["event"] == "error"
    assert "not loaded" in events[0]["message"] and "16384" in events[0]["message"]
    assert not any(e["event"] == "progress" for e in events)
    assert not fake.calls


def test_lm_studio_model_missing_errors_before_work(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    listing = ModelListing(models=[ModelInfo(id="lm_studio/other", loaded=True)])
    h = harness(
        _lms_cfg(tmp_path), client=FakeClient({}), list_models_fn=_lm_listing(listing)
    )
    h.send(_start(md_corpus))
    h.event("done")
    events = h.events()
    assert events[0]["event"] == "error" and "qwen" in events[0]["message"]
    assert not any(e["event"] == "progress" for e in events)


def test_lm_studio_unknown_load_state_emits_notice_and_runs(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    listing = ModelListing(models=[ModelInfo(id="lm_studio/qwen")])
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_lms_cfg(tmp_path), client=fake, list_models_fn=_lm_listing(listing))
    h.send(_start(md_corpus))
    done = h.event("done")
    kinds = [e["event"] for e in h.events()]
    assert kinds[0] == "notice" and "16384" in h.events()[0]["message"]
    assert "progress" in kinds and done["cancelled"] is False


def test_lm_studio_unreachable_or_raising_errors_before_work(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    cfg = _write_config(
        tmp_path,
        extract_provider="lms",
        extract_model="lm_studio/q",
        extra_providers=_LM,
    )

    async def boom(name: str, provider: ProviderConfig, **_: Any) -> ModelListing:
        raise ValueError("bad port")

    h = harness(cfg, client=FakeClient({}), list_models_fn=boom)
    h.send(_start(md_corpus))
    h.event("done")
    events = h.events()
    assert events[0]["event"] == "error" and "bad port" in events[0]["message"]
    assert not any(e["event"] == "progress" for e in events)


def test_run_writes_a_log_file_under_the_corpus(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(md_corpus))
    h.event("done")
    logs = list((md_corpus / ".corpusqa" / "logs").glob("run-*.log"))
    assert len(logs) == 1


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod does not make a folder read-only here",
)
def test_read_only_folder_gives_a_clear_error(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    md_corpus.chmod(0o555)
    try:
        h = harness(_write_config(tmp_path), client=FakeClient({}))
        h.send(_start(md_corpus))
        error = h.event("error")
        done = h.event("done")
    finally:
        md_corpus.chmod(0o755)
    assert "must be writable" in error["message"]
    assert ".corpusqa" in error["message"]
    assert done["cancelled"] is False


def test_second_run_start_is_rejected(
    tmp_path: Path, fake_convert: None, harness: Callable[..., Harness]
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nCommunity gardens.\n", encoding="utf-8")
    fake = _SlowClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(root))
    h.reply("run")
    h.send({**_start(root), "id": "run2"})
    assert "already" in h.reply("run2")["error"]
    h.event("done")


def test_config_get_and_save(tmp_path: Path, harness: Callable[..., Harness]) -> None:
    cfg = _write_config(tmp_path)
    h = harness(cfg)
    h.send({"id": "g", "cmd": "config.get"})
    got = h.reply("g")["result"]
    assert got["path"] == str(cfg)
    assert got["config"]["tasks"]["extract"]["temperature"] == 0.2
    h.send(
        {
            "id": "s",
            "cmd": "config.save",
            "params": {"changes": [[["tasks", "extract", "temperature"], 0.5]]},
        }
    )
    saved = h.reply("s")["result"]
    assert saved["config"]["tasks"]["extract"]["temperature"] == 0.5
    assert "# keep this comment" in cfg.read_text(encoding="utf-8")


def test_config_save_rejects_invalid(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    cfg = _write_config(tmp_path)
    before = cfg.read_text(encoding="utf-8")
    h = harness(cfg)
    h.send(
        {
            "id": "s",
            "cmd": "config.save",
            "params": {"changes": [[["tasks", "extract", "temperature"], 9]]},
        }
    )
    reply = h.reply("s")
    assert "tasks.extract.temperature" in reply["error"]
    assert "tasks.extract.temperature" in reply["fields"]
    assert cfg.read_text(encoding="utf-8") == before


def test_bad_config_at_startup_is_an_event_and_server_keeps_running(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    cfg = tmp_path / "corpusqa.yaml"
    cfg.write_text("providers: {}\ntasks: {}\n", encoding="utf-8")
    h = harness(cfg)
    assert "missing task configs" in h.event("error")["message"]
    h.send({"id": "g", "cmd": "config.get"})
    assert "missing task configs" in h.reply("g")["error"]


def test_models_list_reply_shape_and_errors(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    listing = ModelListing(models=[ModelInfo(id="ollama/a")])

    async def fn(name: str, provider: ProviderConfig, **_: Any) -> ModelListing:
        if name == "local":
            return listing
        raise AssertionError("unexpected")

    h = harness(_write_config(tmp_path), list_models_fn=fn)
    h.send({"id": "m", "cmd": "models.list", "params": {"provider": "local"}})
    assert h.reply("m")["result"] == {
        "models": [
            {
                "id": "ollama/a",
                "loaded": None,
                "loaded_context": None,
                "max_context": None,
            }
        ],
        "error": None,
    }
    h.send({"id": "n", "cmd": "models.list", "params": {"provider": "nope"}})
    result = h.reply("n")["result"]
    assert result["models"] == [] and "nope" in result["error"]


def test_models_list_exception_becomes_error(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    async def boom(name: str, provider: ProviderConfig, **_: Any) -> ModelListing:
        raise ValueError("Port could not be cast to integer value")

    h = harness(_write_config(tmp_path), list_models_fn=boom)
    h.send({"id": "m", "cmd": "models.list", "params": {"provider": "local"}})
    result = h.reply("m")["result"]
    assert result["models"] == [] and "Port" in result["error"]


def test_doc_get_returns_markdown_and_page_map(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(md_corpus))
    h.event("done")
    h.send(
        {
            "id": "d",
            "cmd": "doc.get",
            "params": {"folder": str(md_corpus), "rel_path": "gardens.md"},
        }
    )
    result = h.reply("d")["result"]
    assert QUOTE in result["markdown"] and result["page_map"] == [[0, "1"]]
    h.send(
        {
            "id": "e",
            "cmd": "doc.get",
            "params": {"folder": str(md_corpus), "rel_path": "nope.md"},
        }
    )
    assert "nope.md" in h.reply("e")["error"]


def test_bad_lines_and_unknown_commands(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    h = harness(_write_config(tmp_path))
    h._in.put("not json\n")
    assert "invalid request" in h.event("error")["message"]
    h.send({"id": "u", "cmd": "nope"})
    assert "unknown command" in h.reply("u")["error"]


_LEAKY = (
    "import os, sys\n"
    "from corpusqa.gui_backend import server\n"
    "real = server.Server._startup\n"
    "def leaky(self):\n"
    "    print('LEAK from print')\n"
    "    os.write(1, b'LEAK from fd 1\\n')\n"
    "    real(self)\n"
    "server.Server._startup = leaky\n"
    "raise SystemExit(server.main(sys.argv[1:]))\n"
)


@pytest.mark.parametrize(
    "entry",
    [
        ["-m", "corpusqa.gui_backend"],
        ["-m", "corpusqa.cli.main", "gui-backend"],
        ["-c", _LEAKY],
    ],
    ids=["module", "cli", "library-output"],
)
def test_stdout_carries_only_protocol_lines(tmp_path: Path, entry: list[str]) -> None:
    cfg = _write_config(tmp_path)
    requests = (
        json.dumps({"id": "1", "cmd": "config.get"})
        + "\n"
        + json.dumps(
            {
                "id": "2",
                "cmd": "doc.get",
                "params": {"folder": "/nope", "rel_path": "x"},
            }
        )
        + "\n"
    )
    proc = subprocess.run(
        [sys.executable, *entry, "--config", str(cfg)],
        input=requests.encode("utf-8"),
        capture_output=True,
        timeout=120,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    lines = proc.stdout.decode("utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["1", "2"]
    if entry[0] == "-c":
        assert "LEAK from print" in proc.stderr.decode()
        assert "LEAK from fd 1" in proc.stderr.decode()


_ENV_PROBE = (
    "import os, sys\n"
    "from corpusqa.gui_backend import server\n"
    "real = server.Server._startup\n"
    "def probe(self):\n"
    "    real(self)\n"
    "    sys.stderr.write('PROBE=%s\\n' % os.environ.get('CQA_ENV_FILE_PROBE'))\n"
    "server.Server._startup = probe\n"
    "if sys.argv[1] == 'cli':\n"
    "    from corpusqa.cli.main import main\n"
    "    raise SystemExit(main(['gui-backend', *sys.argv[2:]]))\n"
    "raise SystemExit(server.main(sys.argv[2:]))\n"
)


@pytest.mark.parametrize("entry", ["module", "cli"])
def test_env_file_flag_loads_given_file(
    tmp_path: Path, entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CQA_ENV_FILE_PROBE", raising=False)
    cfg = _write_config(tmp_path)
    (cfg.parent / ".env").write_text("CQA_ENV_FILE_PROBE=next-to-config\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "custom.env").write_text("CQA_ENV_FILE_PROBE=1\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _ENV_PROBE,
            entry,
            "--config",
            str(cfg),
            "--env-file",
            str(other / "custom.env"),
        ],
        input=b"",
        capture_output=True,
        timeout=120,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert "PROBE=1\n" in proc.stderr.decode()


_REMOTE = (
    "  remote:\n"
    '    api_base: "https://example.invalid/v1"\n'
    "    api_key_env: MISSING_KEY\n"
)


def _route_keyless(tmp_path: Path, **kwargs: Any) -> Path:
    return _write_config(
        tmp_path,
        route_provider="remote",
        route_model="openai/x",
        extra_providers=_REMOTE,
        **kwargs,
    )


def test_sweep_ignores_missing_key_of_unused_route_tasks(
    md_corpus: Path,
    tmp_path: Path,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_route_keyless(tmp_path), client=fake)
    h.send(_start(md_corpus, mode="sweep"))
    done = h.event("done")
    assert done["cancelled"] is False
    assert not [e for e in h.events() if e["event"] == "error"]
    assert len(fake.calls[TaskName.EXTRACT]) == 1


def test_route_mode_with_missing_route_key_errors_first(
    md_corpus: Path,
    tmp_path: Path,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    fake = FakeClient({})
    h = harness(_route_keyless(tmp_path), client=fake)
    h.send(_start(md_corpus, mode="route"))
    h.event("done")
    events = h.events()
    assert events[0]["event"] == "error" and "MISSING_KEY" in events[0]["message"]
    assert not any(e["event"] == "progress" for e in events)
    assert not fake.calls


def test_auto_resolving_to_route_checks_key_after_conversion(
    md_corpus: Path,
    tmp_path: Path,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    fake = FakeClient({})
    # threshold 0: one parseable file is above it, so auto resolves to route
    h = harness(_route_keyless(tmp_path, sweep_threshold=0), client=fake)
    h.send(_start(md_corpus, mode="auto"))
    done = h.event("done")
    kinds = [e["event"] for e in h.events()]
    phases = [e["phase"] for e in h.events() if e["event"] == "progress"]
    assert phases and set(phases) == {"converting"}  # conversion ran
    error = next(e for e in h.events() if e["event"] == "error")
    assert "MISSING_KEY" in error["message"]
    assert kinds.index("error") > kinds.index("progress")
    assert not fake.calls  # no catalog, routing or extraction call
    assert done["cancelled"] is False


def test_auto_resolving_to_sweep_ignores_route_key(
    md_corpus: Path,
    tmp_path: Path,
    harness: Callable[..., Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_route_keyless(tmp_path), client=fake)
    h.send(_start(md_corpus, mode="auto"))
    assert h.event("done")["cancelled"] is False
    assert not [e for e in h.events() if e["event"] == "error"]


def test_lm_studio_preflight_skips_unused_tasks(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    cfg = _write_config(
        tmp_path,
        route_provider="lms",
        route_model="lm_studio/qwen",
        extra_providers=_LM,
    )
    listing = ModelListing(
        models=[ModelInfo(id="lm_studio/qwen", loaded=True, loaded_context=4096)]
    )
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(cfg, client=fake, list_models_fn=_lm_listing(listing))
    h.send(_start(md_corpus, mode="sweep"))
    assert h.event("done")["cancelled"] is False
    assert not [e for e in h.events() if e["event"] in ("error", "notice")]


def test_run_saves_a_results_file_and_reports_its_path(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(md_corpus, definition="community food"))
    done = h.event("done")
    path = Path(done["results_path"])
    assert path.parent == md_corpus / ".corpusqa" / "results"

    h.send({"id": "l", "cmd": "results.list", "params": {"folder": str(md_corpus)}})
    listed = h.reply("l")["result"]["results"]
    assert [r["path"] for r in listed] == [str(path)]
    assert listed[0]["outcome"] == "finished" and listed[0]["findings"] == 1
    assert listed[0]["definition"] == "community food"

    h.send({"id": "o", "cmd": "results.load", "params": {"path": str(path)}})
    loaded = h.reply("o")["result"]
    run = loaded["run"]
    assert run["question"] == "what ideas exist?"
    assert run["definition"] == "community food"
    assert run["files"][0]["findings"][0]["quote"] == QUOTE
    assert loaded["sources"] == {"gardens.md": "same"}

    h.send(
        {
            "id": "d",
            "cmd": "doc.get",
            "params": {"folder": str(md_corpus), "rel_path": "gardens.md"},
        }
    )
    assert h.reply("d")["result"]["file_hash"] == run["files"][0]["file_hash"]


def test_results_load_flags_changed_and_missing_sources(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    (md_corpus / "rivers.md").write_text("# Rivers\n\nRivers run.\n", encoding="utf-8")
    fake = FakeClient({TaskName.EXTRACT: [_extraction(), _extraction(relevant=False)]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(md_corpus))
    path = h.event("done")["results_path"]
    # One source edited, one deleted, then the corpus reconverted.
    (md_corpus / "gardens.md").write_text("# Garden\n\nChanged.\n", encoding="utf-8")
    (md_corpus / "rivers.md").unlink()
    pipeline.run_index(md_corpus, load_config(_write_config(tmp_path)))
    h.send({"id": "o", "cmd": "results.load", "params": {"path": path}})
    assert h.reply("o")["result"]["sources"] == {
        "gardens.md": "changed",
        "rivers.md": "missing",
    }


def test_results_load_reports_a_bad_file(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    h = harness(_write_config(tmp_path), client=FakeClient({}))
    h.send({"id": "o", "cmd": "results.load", "params": {"path": str(bad)}})
    assert "not a corpusqa results file" in h.reply("o")["error"]


def test_group_start_groups_a_saved_run(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    grouped = json.dumps({"groups": [{"answer": "Community gardens", "hits": [1]}]})
    fake = FakeClient(
        {TaskName.EXTRACT: [_extraction()], TaskName.SYNTHESIZE: [grouped]}
    )
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(md_corpus))
    path = h.event("done")["results_path"]
    h.clear()

    h.send({"id": "g", "cmd": "group.start", "params": {"path": path}})
    assert h.reply("g")["result"] == {"started": True}
    done = h.event("done")
    assert done["results_path"] == path and done["cancelled"] is False
    phases = {e["phase"] for e in h.events() if e["event"] == "progress"}
    assert phases == {"grouping"}

    h.send({"id": "o", "cmd": "results.load", "params": {"path": path}})
    grouping = h.reply("o")["result"]["run"]["grouping"]
    assert grouping["groups"][0]["answer"] == "Community gardens"
    assert grouping["groups"][0]["hits"] == [{"file": "gardens.md", "index": 0}]


def test_group_start_reports_a_bad_results_file(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    h = harness(_write_config(tmp_path), client=FakeClient({}))
    h.send({"id": "g", "cmd": "group.start", "params": {"path": str(tmp_path / "x")}})
    assert "cannot read" in h.reply("g")["error"]
