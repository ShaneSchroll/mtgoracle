"""Saved chat history for the sidebar archive."""

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth
from .config import MAX_MESSAGES, MAX_MESSAGE_CHARS

router = APIRouter()


# ----- Conversation history (sidebar archive; capped per user in auth.py) -----

class ConvMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class ConvSave(BaseModel):
    id: int | None = None
    title: str = Field(min_length=1, max_length=200)
    format: str = Field(default="Commander", max_length=40)
    messages: list[ConvMessage] = Field(min_length=1, max_length=MAX_MESSAGES)


@router.get("/api/conversations")
def conversations_list(user=Depends(auth.require_user)):
    return {"conversations": auth.list_conversations(user["id"])}


@router.post("/api/conversations")
def conversations_save(req: ConvSave, request: Request, user=Depends(auth.require_user)):
    auth.require_same_origin(request)
    messages_json = json.dumps(
        [m.model_dump() for m in req.messages], ensure_ascii=False
    )
    cid = auth.save_conversation(
        user["id"], req.id, req.title.strip()[:200], req.format, messages_json
    )
    return {"id": cid}


@router.get("/api/conversations/{conv_id}")
def conversations_get(conv_id: int, user=Depends(auth.require_user)):
    conv = auth.get_conversation(user["id"], conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    conv["messages"] = json.loads(conv["messages"])
    return conv


@router.delete("/api/conversations/{conv_id}")
def conversations_delete(conv_id: int, request: Request, user=Depends(auth.require_user)):
    auth.require_same_origin(request)
    if not auth.delete_conversation(user["id"], conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"ok": True}
