"""House agents' forum voice: the code fixes the facts, a small model only words them.

    POST_LLM=1  PROXY_BASE=http://.../v1  PROXY_MODEL=<model>  [PROXY_API_KEY=...]   (any OpenAI-compatible endpoint)

say(fallback, brief, must) asks the model for one forum post in the agent's own tone (picked from its name, so it
stays the same across runs). The answer is used only when it keeps every string in `must` (what other house code
parses, such as "구합니다" and the category name), fits in 400 characters and has no number the brief lacks (a
made-up price); otherwise, or with POST_LLM unset or on any error, the fixed sentence `fallback` is posted.
Negotiation never goes through here.
"""
import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

TONES = ["짧고 무뚝뚝하게", "친근하고 수다스럽게", "정중한 존댓말로", "이모티콘 하나 섞어 가볍게", "장사꾼처럼 넉살 좋게",
         "까다로운 단골처럼 깐깐하게", "급한 사람처럼 짧게", "꼼꼼하게 숫자를 앞세워"]
PROMPT = """너는 온라인 장터 게시판에 글을 쓰는 {who}다. 말투: {tone}.
아래 내용을 게시글 하나로 써라. 내용을 바꾸거나 없는 사실을 지어내지 마라. 가격, 수량, 시간 같은 숫자는 내용에 있는 것만 쓴다.
{must}말투 지시 문구는 글에 쓰지 마라. 글만 출력하고, 따옴표나 설명은 붙이지 마라. 200자 이내.

내용: {brief}"""


def tone():
    name = Path.cwd().name
    return TONES[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(TONES)]


def say(fallback, brief, must=(), who="손님"):
    if os.environ.get("POST_LLM") != "1" or not os.environ.get("PROXY_BASE") or not os.environ.get("PROXY_MODEL"):
        return fallback
    must_line = "".join(f'"{m}"라는 말을 글에 그대로 넣어라. ' for m in must)
    body = {"model": os.environ["PROXY_MODEL"],
            "messages": [{"role": "user", "content": PROMPT.format(who=who, tone=tone(), brief=brief,
                                                                   must=must_line + "\n" if must_line else "")}]}
    req = urllib.request.Request(os.environ["PROXY_BASE"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"authorization": "Bearer " + os.environ.get("PROXY_API_KEY", ""),
                                          "content-type": "application/json", "user-agent": "w07-house/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = (json.load(r)["choices"][0]["message"]["content"] or "").strip().strip('"')
    except Exception as e:
        print(f"[voice] {type(e).__name__}: fallback", flush=True)
        return fallback
    invented = set(re.findall(r"\d+", text.replace(",", ""))) - set(re.findall(r"\d+", brief.replace(",", "")))
    if not text or len(text) > 400 or any(m not in text for m in must) or invented:
        print("[voice] unusable answer: fallback", flush=True)
        return fallback
    return text
