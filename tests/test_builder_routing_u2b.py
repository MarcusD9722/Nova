"""The project builder must actually GO THROUGH the ModelRouter.

U2 registered a cloud handle and routed coder+planner to it, but ProjectBuilder
took the local runtime directly — so the `coder` role had no consumer and
enabling cloud changed nothing about project building. These tests assert the
wiring end-to-end so that regression can't come back silently.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator.model_router import ModelHandle, ModelRouter
from core.project_builder import ProjectBuilder

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class TaggedLLM:
    """Records which model was asked, so routing is observable."""
    def __init__(self, tag):
        self.tag = tag
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        return '{"summary": "from ' + self.tag + '"}'


async def main():
    local, cloud = TaggedLLM("local"), TaggedLLM("cloud")
    local_sem, cloud_sem = asyncio.Semaphore(1), asyncio.Semaphore(4)

    # ── No router wired: everything stays local (previous behavior, unchanged) ──
    pb = ProjectBuilder(projects_dir=Path("."), llm=local, llm_semaphore=local_sem, memory=None)
    r, s = pb._handle("coder")
    check(r is local and s is local_sem, "no router -> coder falls back to the local model")
    check(pb._handle("planner")[0] is local, "no router -> planner falls back to the local model")

    # ── Cloud registered, coder+planner routed (what NOVA_CLOUD_ENABLED does) ──
    router = ModelRouter(
        {"primary": ModelHandle("primary", local, local_sem),
         "cloud": ModelHandle("cloud", cloud, cloud_sem)},
        default="primary",
        role_map={"coder": "cloud", "planner": "cloud"},
    )
    pb2 = ProjectBuilder(projects_dir=Path("."), llm=local, llm_semaphore=local_sem,
                         memory=None, models=router)

    check(pb2._handle("coder")[0] is cloud, "coder role resolves to the CLOUD model")
    check(pb2._handle("planner")[0] is cloud, "planner role resolves to the CLOUD model")
    check(pb2._handle("coder")[1] is cloud_sem,
          "the CLOUD semaphore travels with it (remote work doesn't block the GPU)")

    # ── The real call paths must use those handles, not self._llm ──
    await pb2._llm_json("plan something")
    check(cloud.calls == 1 and local.calls == 0, "_llm_json (planning) goes to the cloud model")

    await pb2._llm_file("write a file")
    check(cloud.calls == 2 and local.calls == 0, "_llm_file (code generation) goes to the cloud model")

    # ── An explicit override sends codegen back to local, planning stays remote ──
    split = ModelRouter(
        {"primary": ModelHandle("primary", local, local_sem),
         "cloud": ModelHandle("cloud", cloud, cloud_sem)},
        default="primary",
        role_map={"planner": "cloud"},   # coder deliberately left local
    )
    pb3 = ProjectBuilder(projects_dir=Path("."), llm=local, llm_semaphore=local_sem,
                         memory=None, models=split)
    check(pb3._handle("coder")[0] is local, "unrouted coder stays on the local model")
    check(pb3._handle("planner")[0] is cloud, "routed planner still goes remote")

    # ── A broken router must degrade to local, never crash a build ──
    class BrokenRouter:
        def for_role(self, role):
            raise RuntimeError("router exploded")

    pb4 = ProjectBuilder(projects_dir=Path("."), llm=local, llm_semaphore=local_sem,
                         memory=None, models=BrokenRouter())
    check(pb4._handle("coder")[0] is local, "router failure falls back to local (build never breaks)")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
