import json, os
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk
from pydantic import BaseModel
import workspace as ws
from agents import PROMPTS, commit_message, get_agent


def auth(authorization: str = Header("")):
    if authorization != f"Bearer {os.environ['APP_TOKEN']}":
        raise HTTPException(401)


app = FastAPI(dependencies=[Depends(auth)])
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


class NewSession(BaseModel):
    branch: str | None = None


class Chat(BaseModel):
    session_id: str
    agent: str
    message: str


class Push(BaseModel):
    message: str | None = None


def _check(sid: str):
    if not ws.path(sid).is_dir():
        raise HTTPException(404, "session not found")


@app.post("/sessions")
def new_session(body: NewSession):
    repo = os.environ["WORK_REPO"]
    try:
        return {"session_id": ws.clone(repo, body.branch), "repo": repo}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/chat")
async def chat(body: Chat):
    _check(body.session_id)
    if body.agent not in PROMPTS:
        raise HTTPException(400, "unknown agent")
    agent = get_agent(body.session_id, body.agent)
    cfg = {"configurable": {"thread_id": f"{body.session_id}:{body.agent}"}, "recursion_limit": 100}

    async def stream():
        try:
            async for msg, _ in agent.astream({"messages": [("user", body.message)]}, cfg, stream_mode="messages"):
                if isinstance(msg, AIMessageChunk):
                    if msg.text:
                        yield f"data: {json.dumps({'text': msg.text})}\n\n"
                    for tc in msg.tool_call_chunks:
                        if tc.get("name"):
                            yield f"data: {json.dumps({'tool': tc['name']})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/sessions/{sid}/files")
def get_files(sid: str):
    _check(sid)
    return {"files": ws.files(sid)}


@app.get("/sessions/{sid}/diff")
def get_diff(sid: str):
    _check(sid)
    return {"diff": ws.diff(sid)}


@app.post("/sessions/{sid}/push")
def push(sid: str, body: Push):
    _check(sid)
    try:
        message = body.message or commit_message(ws.diff(sid))
        return {"branch": ws.push(sid, message), "message": message}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/sessions/{sid}/reset")
def reset(sid: str):
    _check(sid)
    try:
        ws.reset(sid)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
