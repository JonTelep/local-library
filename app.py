#!/usr/bin/env python3
"""Local Library: a chat page that answers questions from an offline Wikipedia.

kiwix-serve finds the articles, Ollama writes the answer from them, and the
articles themselves are served from this same port (proxied from kiwix-serve).
Set TYPESAFE_API_KEY to let Jev re-rank the search results and pick the passages.
Standard library only.
"""
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

KIWIX = os.environ.get("KIWIX_URL", "http://127.0.0.1:8080")
BOOK = os.environ.get("KIWIX_BOOK", "wikipedia")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("MODEL", "qwen3.5:4b")
JEV_KEY = os.environ.get("TYPESAFE_API_KEY", "")
PORT = int(os.environ.get("PORT", "8000"))
TOP_ARTICLES, TOP_PASSAGES = 3, 8
INDEX = (Path(__file__).parent / "index.html").read_bytes()
STOP = set("""a an the of in on at to for and or but is are was were be been being by with from as that this
these those it its what who whom whose when where why how which did do does done about tell me you your i
my we our can could would should will shall has have had there their they them he she his her not no""".split())

PLAN_SCHEMA = {"type": "object", "required": ["question", "search"], "properties": {
    "question": {"type": "string"}, "search": {"type": "string"}}}
PLAN_PROMPT = """Turn the user's latest message into a Wikipedia lookup.
Reply with JSON:
- "question": the latest message rewritten as a standalone question (resolve "he", "that war", etc. from the chat).
- "search": 2-6 keywords for a Wikipedia full-text search. Use names, places, events, and years; no filler words."""
ANSWER_PROMPT = """You answer questions using only the numbered Wikipedia excerpts below.
Cite sources inline like [1] or [2]. If the excerpts don't contain the answer, say so plainly instead of guessing.
Be concise: a short paragraph or a few bullet points.

{context}"""


def post(url, body, headers={}, timeout=600):
    req = Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    return urlopen(req, timeout=timeout)


def chat(messages, **extra):
    body = {"model": MODEL, "messages": messages, "stream": False, "think": False,
            "options": {"num_ctx": 8192}, **extra}
    return json.load(post(f"{OLLAMA}/api/chat", body))["message"]["content"]


def chat_stream(messages):
    body = {"model": MODEL, "messages": messages, "think": False, "options": {"num_ctx": 8192}}
    for line in post(f"{OLLAMA}/api/chat", body):
        if token := json.loads(line).get("message", {}).get("content"):
            yield token


def search(query, n=20):
    url = f"{KIWIX}/search?books.name={BOOK}&format=xml&pageLength={n}&pattern={quote(query)}"
    items = ET.fromstring(urlopen(url, timeout=30).read()).iter("item")
    return [{"title": i.findtext("title"), "path": i.findtext("link"),
             "snippet": " ".join("".join(i.find("description").itertext()).split())} for i in items]


def paragraphs(path):
    return extract_paragraphs(urlopen(KIWIX + path, timeout=30).read().decode())


def extract_paragraphs(page):
    """Plain-text paragraphs of an article. Kiwix pages are clean MediaWiki HTML, so a regex is enough."""
    out = []
    for p in re.findall(r"<p\b[^>]*>(.*?)</p>", page, re.S):
        p = re.sub(r"<(sup|style)\b.*?</\1>", "", p, flags=re.S)  # drop [1] footnote markers and inline CSS
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", p)).split())
        if len(text) > 60:
            out.append(text)
    return out[:150]


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2}


def keyword_scores(query, texts):
    q = words(query)
    return [len(q & words(t)) for t in texts]


def jev_scores(question, texts, kind):
    """One Jev request: probability that each text helps answer the question."""
    questions = {str(i): {
        "type": "noul",
        "instructions": {kind: t, "question": f"Does `{kind}` contain information that helps answer `user_question`?"},
        "criteria": {"true": f"The {kind} states facts that directly answer the question or are clearly needed to answer it.",
                     "false": f"The {kind} is about something else, or only shares some words with the question."},
    } for i, t in enumerate(texts)}
    r = post("https://api.typesafe.ai/v1/systemone",
             {"model": "jev-latest", "state": {"user_question": question}, "questions": questions},
             {"Authorization": f"Bearer {JEV_KEY}"}, timeout=60)
    answers = json.load(r)["answers"]
    return [answers[str(i)]["noul"] for i in range(len(texts))]


def top(items, scores, n):
    return [item for _, item in sorted(zip(scores, items), key=lambda s: -s[0])[:n]]


def answer(message, history, emit):
    emit(status="Searching Wikipedia…")
    plan = json.loads(chat([{"role": "system", "content": PLAN_PROMPT}, *history,
                            {"role": "user", "content": message}], format=PLAN_SCHEMA))
    question = plan["question"] or message
    seen, found = set(), []
    for c in search(plan["search"]) + search(question):  # keyword query first, whole question widens recall
        if c["path"] not in seen:
            seen.add(c["path"])
            found.append(c)
    if not found:
        emit(token="I couldn't find anything about that in the offline Wikipedia.")
        return

    use_jev = bool(JEV_KEY)
    if use_jev:
        emit(status=f"Jev is re-ranking {len(found)} articles…")
        try:
            articles = top(found, jev_scores(question, [f"{c['title']}: {c['snippet']}" for c in found], "search_result"),
                           TOP_ARTICLES)
        except Exception as e:  # offline or API trouble: fall back to kiwix's own ranking
            emit(status=f"Jev unavailable ({e}), using keyword ranking…")
            use_jev = False
    if not use_jev:
        articles = found[:TOP_ARTICLES]

    emit(status="Reading " + ", ".join(a["title"] for a in articles) + "…")
    with ThreadPoolExecutor() as pool:
        paras = list(pool.map(lambda a: paragraphs(a["path"]), articles))
        # (source number, paragraph index, text); the lead paragraph always counts as a candidate
        candidates = [(n, i, t) for n, ps in enumerate(paras, 1) for i, t in enumerate(ps)]
        scores = None
        if use_jev:
            try:
                per_article = pool.map(lambda ps: jev_scores(question, ps, "passage") if ps else [], paras)
                scores = [s for article in per_article for s in article]
            except Exception as e:
                emit(status=f"Jev unavailable ({e}), using keyword ranking…")
        if scores is None:
            scores = [k + (0.5 if i == 0 else 0) for k, (_, i, _) in
                      zip(keyword_scores(question + " " + plan["search"], [t for *_, t in candidates]), candidates)]
    chosen = sorted(top(candidates, scores, TOP_PASSAGES))  # back into article order so excerpts read naturally

    emit(sources=[{"n": n, "title": a["title"], "path": a["path"]} for n, a in enumerate(articles, 1)], jev=use_jev)
    context = "\n\n".join(f"[{n}] {articles[n - 1]['title']}: {t}" for n, _, t in chosen)
    for token in chat_stream([{"role": "system", "content": ANSWER_PROMPT.format(context=context)}, *history,
                              {"role": "user", "content": question}]):
        emit(token=token)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            type_, body = "text/html; charset=utf-8", INDEX
        else:  # everything else is a Wikipedia page or asset: pass it through from kiwix-serve
            try:
                r = urlopen(KIWIX + self.path, timeout=30)
            except HTTPError as e:
                return self.send_error(e.code)
            type_, body = r.headers.get("Content-Type", "application/octet-stream"), r.read()
        self.send_response(200)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/ask":
            return self.send_error(404)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 200_000)))
            message = str(body["message"])[:2000]
            history = [{"role": "assistant" if m["role"] == "assistant" else "user", "content": str(m["content"])[:4000]}
                       for m in body.get("history", [])[-6:]]
        except (ValueError, KeyError, TypeError):
            return self.send_error(400)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def emit(**msg):
            self.wfile.write((json.dumps(msg) + "\n").encode())
            self.wfile.flush()

        try:
            answer(message, history, emit)
        except BrokenPipeError:
            pass
        except Exception as e:
            emit(error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"Local Library on :{PORT} (model {MODEL}, Jev {'on' if JEV_KEY else 'off'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
