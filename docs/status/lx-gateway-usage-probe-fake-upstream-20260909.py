"""假上游：精确控制是否返回 usage，用来区分'透传'与'本地估算'。"""
import json, time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
# 故意用一个好认的数字，真实透传的话钩子里应当原样出现
SENTINEL = {"prompt_tokens": 1111, "completion_tokens": 2222, "total_tokens": 3333,
            "prompt_tokens_details": {"cached_tokens": 999}}
BODY = "hello world from the fake upstream, this sentence has quite a few tokens in it"


def _base():
    return {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
            "model": "fake-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": BODY}}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    payload = await request.json()
    mode = payload.get("model", "")
    stream = bool(payload.get("stream"))

    if not stream:
        body = _base()
        if "nousage" not in mode:
            body["usage"] = SENTINEL
        # "nousage" 模式：完全不返回 usage 字段
        return JSONResponse(body)

    def gen():
        chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": "fake-model",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": BODY}}]}
        yield f"data: {json.dumps(chunk)}\n\n"
        fin = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
               "created": int(time.time()), "model": "fake-model",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        if "nousage" not in mode:
            fin["usage"] = SENTINEL
        yield f"data: {json.dumps(fin)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
