"""The showcase's web app: the page, the scenarios with their cached runs, live runs and the free question.

    uvicorn showcase.app:from_env --factory   (configuration: showcase/README.md)
"""

import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from showcase.agents import Agents, Run, Settings, compare, prices
from showcase.data import TODAY, customers
from showcase.rag import Index
from showcase.scenarios import SCENARIOS

STATIC = Path(__file__).resolve().parent / "static"


class Limits:
    """Daily counters in memory: per visitor for free questions, and global caps for questions and live runs."""

    def __init__(self, ask_per_ip: int, ask_per_day: int, live_per_day: int) -> None:
        self.caps = {"ask_ip": ask_per_ip, "ask": ask_per_day, "live": live_per_day}
        self.day, self.counts = "", {}
        self.lock = threading.Lock()

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day:
            self.day, self.counts = today, {}

    def left(self, kind: str, ip: str = "") -> int:
        with self.lock:
            self._roll()
            if kind == "ask":
                return max(0, min(self.caps["ask_ip"] - self.counts.get(("ask_ip", ip), 0), self.caps["ask"] - self.counts.get("ask", 0)))
            return max(0, self.caps["live"] - self.counts.get("live", 0))

    def take(self, kind: str, ip: str = "") -> bool:
        if self.left(kind, ip) <= 0:
            return False
        with self.lock:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if kind == "ask":
                self.counts[("ask_ip", ip)] = self.counts.get(("ask_ip", ip), 0) + 1
        return True


class Showcase:
    def __init__(self, agents: Agents, cache_path: Path, limits: Limits) -> None:
        self.agents, self.cache_path, self.limits = agents, cache_path, limits
        self.names = {c["tenant"]: c["name"] for c in customers()}
        self.cache: dict[str, Any] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.lock = threading.Lock()

    def _both(self, fn: Any) -> tuple[Any, Any]:
        with ThreadPoolExecutor(2) as pool:  # the two sides run at the same time, so neither waits for the other
            k, r = pool.submit(fn, "keel"), pool.submit(fn, "rag")
            return k.result(), r.result()

    def ask(self, tenant: str, question: str) -> dict[str, Any]:
        if tenant not in self.names:
            raise ValueError(f"unknown customer '{tenant}'")
        question = question.strip()[:500]
        if not question:
            raise ValueError("ask a question")
        keel, rag = self._both(lambda side: self.agents.answer(side, Run(tenant, self.names[tenant]), [], question)[0])
        return {"at": datetime.now(timezone.utc).isoformat(), "keel": keel, "rag": rag, "compare": compare(keel, rag)}

    def run(self, sid: str) -> dict[str, Any]:
        sc = next((s for s in SCENARIOS if s["id"] == sid), None)
        if sc is None:
            raise LookupError(f"unknown scenario '{sid}'")
        out = self._memory(sc) if sc.get("steps") else self._single(sc)
        with self.lock:
            self.cache[sid] = out
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False))
        return out

    def _single(self, sc: dict[str, Any]) -> dict[str, Any]:
        def side(name: str) -> dict[str, Any]:
            result = self.agents.answer(name, Run(sc["customer"], self.names[sc["customer"]]), [], sc["question"])[0]
            if not result.get("error"):
                result["verdict"] = self.agents.judge(sc["question"], sc["expected"], result["answer"])
            return result

        keel, rag = self._both(side)
        return {"at": datetime.now(timezone.utc).isoformat(), "keel": keel, "rag": rag, "compare": compare(keel, rag)}

    def _memory(self, sc: dict[str, Any]) -> dict[str, Any]:
        """The steps in order, each side keeping its own memory: Keel in a task of its own for this run, RAG in its notes."""
        task = f"showcase-{secrets.token_hex(4)}"

        def side(name: str) -> list[dict[str, Any]]:
            runs: dict[str, Run] = {}  # per customer: notes and task memory never cross customers
            history: list[dict[str, Any]] = []
            results = []
            for step in sc["steps"]:
                t = step["customer"]
                run = runs.setdefault(t, Run(t, self.names[t], task=task))
                if step.get("new_conversation"):
                    history = []
                result, history = self.agents.answer(name, run, history, step["question"], memory=True)
                results.append(result)
            return results

        keel_steps, rag_steps = self._both(side)
        steps = [{"customer": s["customer"], "question": s["question"], "keel": k, "rag": r, "compare": compare(k, r)}
                 for s, k, r in zip(sc["steps"], keel_steps, rag_steps, strict=True)]
        transcript = lambda key: "\n\n".join(f"Step {i + 1} ({s['customer']}): {st[key]['answer']}" for i, (s, st) in enumerate(zip(sc["steps"], steps, strict=True)))
        question = " / ".join(s["question"] for s in sc["steps"])
        totals = {}
        for key in ("keel", "rag"):
            all_ = [st[key] for st in steps]
            totals[key] = {"answer": all_[-1]["answer"], "trace": [t for s in all_ for t in s["trace"]], "ms": sum(s["ms"] for s in all_),
                           "tokens": {"input": sum(s["tokens"]["input"] for s in all_), "output": sum(s["tokens"]["output"] for s in all_)},
                           "cost_usd": round(sum(s["cost_usd"] for s in all_), 6), "calls": sum(s["calls"] for s in all_), "verdict": None}
            if not any(s.get("error") for s in all_):
                verdict = self.agents.judge(question, sc["expected"], transcript(key))
                totals[key]["verdict"] = verdict
                steps[-1][key]["verdict"] = verdict
        return {"at": datetime.now(timezone.utc).isoformat(), "keel": totals["keel"], "rag": totals["rag"],
                "compare": compare(totals["keel"], totals["rag"]), "steps": steps}

    def scenarios(self) -> list[dict[str, Any]]:
        out = []
        for s in SCENARIOS:
            item = {k: v for k, v in s.items() if k != "steps"}
            item["customer_name"] = self.names[s["customer"]]
            if s.get("steps"):
                item["steps"] = [{"customer": st["customer"], "customer_name": self.names[st["customer"]], "question": st["question"]} for st in s["steps"]]
                item["question"] = s["steps"][0]["question"]
            item["result"] = self.cache.get(s["id"])
            out.append(item)
        return out


def build_app(show: Showcase) -> Any:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    app = FastAPI(title="keel-showcase", docs_url=None, redoc_url=None, openapi_url=None)

    def ip(request: Request) -> str:
        return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()

    class AskIn(BaseModel):
        customer: str
        question: str

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/meta")
    def meta(request: Request) -> dict[str, Any]:
        return {"today": TODAY.isoformat().replace("+00:00", "Z"), "model": show.agents.s.model, "judge_model": show.agents.s.judge_model,
                "customers": customers(), "limits": {"ask_left": show.limits.left("ask", ip(request)), "live_left": show.limits.left("live")}}

    @app.get("/api/scenarios")
    def scenarios() -> list[dict[str, Any]]:
        return show.scenarios()

    @app.post("/api/scenarios/{sid}/run")
    def run(sid: str) -> dict[str, Any]:
        if not any(s["id"] == sid for s in SCENARIOS):
            raise HTTPException(404, f"unknown scenario '{sid}'")
        if not show.limits.take("live"):
            raise HTTPException(429, "today's live runs are used up: the cached runs are shown; come back tomorrow")
        return show.run(sid)

    @app.post("/api/ask")
    def ask(body: AskIn, request: Request) -> dict[str, Any]:
        if not show.limits.take("ask", ip(request)):
            raise HTTPException(429, "you have used today's questions: come back tomorrow")
        try:
            return show.ask(body.customer, body.question)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app


def from_env() -> Any:
    import httpx

    env = os.environ
    s = Settings(key=env["OFOX_API_KEY"], model=env.get("SHOWCASE_MODEL", "openai/gpt-6-sol"), judge_model=env.get("SHOWCASE_JUDGE_MODEL", "anthropic/claude-sonnet-5.5"),
                 keel_url=env["KEEL_URL"].rstrip("/"), keel_key=env.get("KEEL_KEY") or None)
    http = httpx.Client(timeout=180, headers={"User-Agent": "keel-showcase"})
    data = Path(env.get("SHOWCASE_DATA", "/data"))
    index = Index.load_or_build(data / "index.json", http, s.base_url, s.key)
    agents = Agents(http, s, index, prices(http, s))
    limits = Limits(int(env.get("SHOWCASE_ASK_PER_IP", "5")), int(env.get("SHOWCASE_ASK_PER_DAY", "200")), int(env.get("SHOWCASE_LIVE_PER_DAY", "40")))
    return build_app(Showcase(agents, data / "cache.json", limits))
