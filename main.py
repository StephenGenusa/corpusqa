import asyncio
from pathlib import Path
from pydantic import BaseModel
from corpusqa.config import load_config, TaskName
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.llm.structured import complete_structured

class Probe(BaseModel):
    greeting: str
    number: int

cfg = load_config(Path("corpusqa.yaml"))
client = LLMTaskClient(cfg)
out = asyncio.run(complete_structured(
    client, TaskName.QUERY_ROUTE,
    [{"role": "user", "content": "Tell me about yourself and pick a random number."}], Probe))
print(out)
