#!/usr/bin/env python3
"""BaalSTT — public Speech-to-Text service powered by Deepgram $200 credit.

Endpoint:
  POST /transcribe
    multipart:  file=<audio>            (mp3/wav/m4a/ogg/webm/flac, <= 25 MB)
    json:       {"url": "https://..."}  (public audio URL)
    query:      ?model=nova-2|whisper-large-v3  (default nova-2)
  GET /           landing + API docs
  GET /health     liveness + remaining free credit estimate

Free tier: 3 transcriptions / 24 h / IP (no key).
Beyond that: HTTP 402 with payment addresses (BTC + SOL) — pay-what-you-want.
Then POST /verify {"txid": ..., "chain": "auto"} -> we check the REAL chain
(Blockstream / Solana-RPC) that >= MIN reached our wallets -> 72 h API key
sent back that lifts the cap. Manual fallback: txid to baaalai@proton.me.
"""
import json, os, re, time, secrets, urllib.request, urllib.error
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

BASE = Path(os.environ.get("BAALSTT_STATE_DIR", str(Path(__file__).parent)))
IDENT_F = os.environ.get("BAALSTT_IDENTITY", "")
if IDENT_F:
    IDENT = json.load(open(IDENT_F))
else:
    IDENT = {}
DG_KEY = os.environ.get("BAALSTT_DG_KEY") or IDENT["deepgram"]["api_key"]
DG_PROJ = os.environ.get("BAALSTT_DG_PROJECT") or IDENT["deepgram"]["project_id"]
BTC_ADDR = os.environ.get("BAALSTT_BTC") or IDENT["btc_wallet"]["address"]
SOL_ADDR = os.environ.get("BAALSTT_SOL") or IDENT["airdrop_sol_wallet"]["address"]

FREE_PER_DAY = 3
GLOBAL_DAILY_CAP = 150   # total transcriptions/24h across ALL IPs (DoS / credit-burn guard)
MAX_BYTES = 25 * 1024 * 1024
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".webm", ".flac", ".mp4", ".opus"}
MODELS = {"nova-2", "whisper-large-v3", "whisper-large-v3-turbo"}

STATE = {"counts": {}, "global": {"day": "", "n": 0}}  # ip -> {...}; global daily cap

PAYMENTS_F = BASE / "payments.json"   # txid -> {chain, amount, ts, ip}
KEYS_F = BASE / "keys.json"           # api_key -> {created, txid, ip}
MIN_SATS = 5000       # BTC floor (~$2.5)
MIN_SOL = 0.15        # SOL floor (~$1.7)

def _load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d

def _save(p, d):
    t = str(p) + ".tmp"
    json.dump(d, open(t, "w"), indent=2)
    os.replace(t, p)

def get_keys() -> dict:
    return _load(KEYS_F, {})

def get_payments() -> dict:
    return _load(PAYMENTS_F, {})

app = FastAPI(title="BaalSTT", version="0.1.0")

class UrlIn(BaseModel):
    url: str
    model: str = "nova-2"

class VerifyIn(BaseModel):
    txid: str
    chain: str = "auto"   # auto | btc | sol

def day() -> str:
    return time.strftime("%Y-%m-%d")

def client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")

def check_quota(ip: str, paid: bool) -> None:
    today = day()
    rec = STATE["counts"].get(ip, {"day": today, "n": 0, "paid": False})
    if rec.get("day") != today:
        rec = {"day": today, "n": 0, "paid": rec.get("paid", False)}
    if paid:
        rec["paid"] = True
    # global daily cap (DoS / credit-burn guard) — applies to everyone incl. paid
    g = STATE["global"]
    if g.get("day") != today:
        g["day"] = today
        g["n"] = 0
    g["n"] += 1
    if g["n"] > GLOBAL_DAILY_CAP:
        raise HTTPException(status_code=503, detail={
            "reason": f"global daily cap reached ({GLOBAL_DAILY_CAP}/24h)",
            "note": "service is rate-limited for today to protect the credit pool; retry tomorrow",
        })
    STATE["global"] = g
    if not paid:
        rec["n"] += 1
        if rec["n"] > FREE_PER_DAY:
            raise HTTPException(status_code=402, detail={
                "reason": f"free tier exhausted ({FREE_PER_DAY}/24h)",
                "pay_what_you_want": {"btc": BTC_ADDR, "sol": SOL_ADDR},
                "min_suggestion_usd": 2,
                "verify": "POST /verify  {\"txid\": \"<deine_txid>\", \"chain\": \"auto\"} — API-Key zurück",
                "manual": "oder Txid an baaalai@proton.me",
            })
    STATE["counts"][ip] = rec

def verify_btc(txid: str) -> dict:
    """Blockstream public API: is txid confirmed + does it pay our BTC address?"""
    r = json.load(urllib.request.urlopen(
        f"https://blockstream.info/api/tx/{txid}", timeout=30))
    to_us = 0
    for v in r.get("vout", []):
        if (v.get("scriptpubkey_address") or "") == BTC_ADDR:
            to_us += v.get("value", 0)
    return {"found": True, "to_us_sats": to_us,
            "confirmations": (r.get("status") or {}).get("confirmations", 0)}

def verify_sol(txid: str) -> dict:
    """Solana mainnet RPC: did txid send SOL to our address? (pre/post balance delta)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                       "params": [txid, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 0}]}).encode()
    req = urllib.request.Request(
        "https://api.mainnet-beta.solana.com", data=body,
        headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    result = r.get("result")
    if not result:
        return {"found": False, "to_us_sol": 0.0}
    meta = result.get("meta") or {}
    pre, post = meta.get("preBalances"), meta.get("postBalances")
    keys = ((result.get("transaction") or {}).get("message") or {}).get("accountKeys", [])
    def pk(k):
        return k if isinstance(k, str) else (k.get("pubkey") or k.get("account") or "")
    to_us = 0.0
    if pre and post:
        for i, k in enumerate(keys):
            if pk(k) == SOL_ADDR and i < len(pre) and i < len(post):
                d = (post[i] - pre[i]) / 1e9
                if d > 0:
                    to_us += d
    return {"found": True, "to_us_sol": round(to_us, 9)}

def assert_public_url(u: str) -> None:
    """SSRF guard: the url path has Deepgram fetch it server-side. Block
    internal/private/metadata ranges so callers can't reach my own machine."""
    from urllib.parse import urlparse
    host = (urlparse(u).hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="bad url host")
    if host in ("localhost", "0.0.0.0") or host.endswith((".local", ".internal", ".home.arpa")):
        raise HTTPException(status_code=400, detail="non-public host blocked (SSRF)")
    if host.startswith(("10.", "192.168.", "169.254.", "127.")) or host.startswith("172.16"):
        raise HTTPException(status_code=400, detail="private IP range blocked (SSRF)")
    if host.startswith("fd") or host.startswith("fe8") or host.startswith("fc"):
        raise HTTPException(status_code=400, detail="link-local/ULA IP blocked (SSRF)")
    # tailscale 100.64.0.0/10 = my own mesh (keep the service reachable but not the host)
    if host.startswith("100.") and 64 <= int(host.split(".")[1] or 0) <= 127:
        raise HTTPException(status_code=400, detail="tailscale range blocked (SSRF)")

def deepgram_listen(source: dict) -> dict:
    body = json.dumps({
        "model": source.get("model", "nova-2"),
        "smart_format": True,
        "punctuate": True,
        "url": source.get("url"),
    }).encode()
    if not source.get("url"):
        raise HTTPException(status_code=400, detail="no url or file")
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/listen",
        data=body,
        headers={"Authorization": f"Token {DG_KEY}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        code = 502 if e.code >= 500 else 400
        raise HTTPException(status_code=code, detail={"deepgram": detail})
    ch = r.get("results", {}).get("channels", [{}])[0]
    alts = ch.get("alternatives", [{}])
    return {
        "text": alts[0].get("transcript", "") if alts else "",
        "duration_s": ch.get("duration"),
        "model": source.get("model", "nova-2"),
    }

def deepgram_file(data: bytes, model: str, mimetype: str = "audio/wav") -> dict:
    """Upload path: raw audio bytes -> Deepgram v1/listen (non-JSON body)."""
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/listen?model=" + model +
        "&smart_format=true&punctuate=true",
        data=data,
        headers={"Authorization": f"Token {DG_KEY}",
                 "Content-Type": mimetype,
                 "Accept": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=180))
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        code = 502 if e.code >= 500 else 400
        raise HTTPException(status_code=code, detail={"deepgram": detail})
    ch = r.get("results", {}).get("channels", [{}])[0]
    alts = ch.get("alternatives", [{}])
    return {
        "text": alts[0].get("transcript", "") if alts else "",
        "duration_s": ch.get("duration"),
        "model": model,
    }

Landing = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BaalSTT — Speech to Text API</title>
<style>body{font-family:system-ui,Segoe UI,Roboto,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#1a1a2e;background:#fafafa}
code,pre{background:#eee;border-radius:4px;padding:2px 6px;font-size:14px}
pre{padding:12px;overflow:auto}h1{margin-bottom:4px}p.sub{color:#666;margin-top:0}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left;font-size:14px}
#out{white-space:pre-wrap;background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px;min-height:20px}
button{background:#2563eb;color:#fff;border:0;padding:10px 16px;border-radius:6px;font-size:15px;cursor:pointer}
input[type=file]{margin:8px 0}</style></head><body>
<h1>BaalSTT</h1>
<p class="sub">High-accuracy Speech-to-Text (Deepgram nova-2). Free: 3 transcriptions / 24 h / IP.</p>
<h2>API</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Body</th></tr>
<tr><td>POST</td><td>/transcribe</td><td>multipart <code>file</code> (audio, &le;25 MB) — oder JSON <code>{{"url":"https://..."}}</code>, optional <code>model</code></td></tr>
</table>
<h2>Beispiele</h2>
<pre># Datei hochladen
curl -F "file=@mein_audio.mp3" https://BAALSTT_HOST/transcribe

# Per URL
curl -X POST https://BAALSTT_HOST/transcribe -H "Content-Type: application/json" \\
  -d '{{"url":"https://example.com/podcast.mp3","model":"nova-2"}}'</pre>
<h2>Live-Test</h2>
<p>Beispielsprache (Deepgram-Beispiel, ~30 s) transkribieren:</p>
<button onclick="runTest()">Test transkribieren</button>
<div id="out">…</div>
<h2>Pay-what-you-want (über Free-Tier hinaus)</h2>
<p>Mindestens ~2 USD. BTC: <code>BTC_ADDR</code><br>SOL: <code>SOL_ADDR</code><br>
Nach Zahlung: <code>POST /verify</code> mit <code>{{"txid":"...","chain":"auto"}}</code>
→ die API prüft die echte Kette und gibt einen <b>API-Key (72 h)</b> zurück
mit Header <code>X-BaalSTT-Key</code> — danach unbegrenzt.</p>
<p style="color:#999;font-size:12px">BaalSTT v0.1 — powered by Deepgram nova-2</p>
<script>async function runTest(){{
 const el=document.getElementById('out'); el.textContent='läuft… (holt Sample + transkribiert)';
 try{{
   const audio=await (await fetch('/sample')).blob();
   const fd=new FormData(); fd.append('file', audio, 'sample_speech.wav');
   const r=await fetch('/transcribe?model=nova-2',{{method:'POST',body:fd}});
   const j=await r.json();
   el.textContent=(r.ok? j.text : JSON.stringify(j.detail||j))+'\\n\\n['+r.status+' '+(j.duration_s? j.duration_s.toFixed(1)+'s':'')+']';
 }}catch(e){{el.textContent='Fehler: '+e}}}}
</script></body></html>"""

Landing = Landing.replace("BTC_ADDR", BTC_ADDR).replace("SOL_ADDR", SOL_ADDR)

@app.get("/", response_class=HTMLResponse)
async def index():
    return Landing

@app.get("/health")
async def health():
    return {"ok": True, "free_per_day": FREE_PER_DAY, "active_ips": len(STATE["counts"]),
            "models": sorted(MODELS), "ts": time.time()}

@app.get("/sample")
async def sample():
    p = BASE / "sample_speech.wav"
    if not p.exists():
        raise HTTPException(status_code=404, detail="sample not found")
    from fastapi.responses import FileResponse
    return FileResponse(p, media_type="audio/wav", filename="sample_speech.wav")

@app.post("/transcribe")
async def transcribe(req: Request, model: str = "nova-2", file: UploadFile | None = File(None)):
    if model not in MODELS:
        raise HTTPException(status_code=400, detail=f"model must be one of {sorted(MODELS)}")
    ip = client_ip(req)
    key = (req.headers.get("x-baalstt-key") or "").strip()
    # paid = key exists AND not expired (72 h)
    paid = False
    if key:
        meta = get_keys().get(key)
        if meta and (time.time() - meta.get("created", 0)) < 72 * 3600:
            paid = True
    check_quota(ip, paid)
    ct = (req.headers.get("content-type") or "").lower()
    if file is not None:
        data = await file.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(status_code=413, detail="file > 25 MB")
        ext = Path(file.filename or "").suffix.lower()
        if ext and ext not in AUDIO_EXT:
            raise HTTPException(status_code=415, detail=f"unsupported type {ext}")
        return deepgram_file(data, model, file.content_type or "application/octet-stream")
    if "multipart/form-data" in ct:
        # multipart ohne Datei: Body bereits von File-Parsing konsumiert
        raise HTTPException(status_code=400, detail="send multipart file or JSON {url}")
    body = await req.body()
    try:
        j = json.loads(body or b"{}")
        u = j.get("url", "")
    except Exception:
        raise HTTPException(status_code=400, detail="send multipart file or JSON {url}")
    if not re.match(r"^https?://", u):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    assert_public_url(u)
    return deepgram_listen({"url": u, "model": model})

@app.post("/verify")
async def verify_payment(req: Request, v: VerifyIn):
    """Customer paid: send txid (+ chain). We check the REAL chain that a
    payment of at least MIN reached our wallets; on success an API key is
    returned that lifts the free-tier cap (valid 72 h)."""
    ip = client_ip(req)
    txid = v.txid.strip()
    chain = v.chain.strip().lower()
    if chain == "auto":
        chain = "btc" if len(txid) == 64 else "sol"
    if chain not in ("btc", "sol"):
        raise HTTPException(status_code=400, detail="chain must be btc|sol|auto")
    try:
        if chain == "btc":
            info = verify_btc(txid)
        else:
            info = verify_sol(txid)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(status_code=404, detail={"txid_not_found": txid,
                "note": "txid not on chain yet — send it again once confirmed"})
        raise HTTPException(status_code=502, detail={"chain_api": str(e)})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"chain_api": str(e)})
    if chain == "btc":
        ok = info.get("found") and info.get("confirmations", 0) >= 1 and info["to_us_sats"] >= MIN_SATS
        amt = {"sats_to_us": info.get("to_us_sats"), "min_sats": MIN_SATS,
               "confirmations": info.get("confirmations")}
    else:
        ok = info.get("found") and info["to_us_sol"] >= MIN_SOL
        amt = {"sol_to_us": info.get("to_us_sol"), "min_sol": MIN_SOL}
    if not ok:
        raise HTTPException(status_code=402, detail={"txid": txid, "chain": chain,
            "detail": amt, "note": "below minimum or no payment to our address in this txid"})
    # payment valid -> mint a 72 h key
    keys = get_keys()
    key = "baal_" + secrets.token_hex(16)
    keys[key] = {"created": time.time(), "txid": txid, "chain": chain, "ip": ip}
    _save(KEYS_F, keys)
    pays = get_payments()
    pays[txid] = {"chain": chain, "amount": amt, "ts": time.time(), "ip": ip, "key": key}
    _save(PAYMENTS_F, pays)
    return {"ok": True, "api_key": key, "valid_hours": 72,
            "note": "send header X-BaalSTT-Key: " + key + " to lift the free-tier cap",
            "amount": amt, "txid": txid, "chain": chain}

@app.exception_handler(HTTPException)
async def http_ex(req, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
