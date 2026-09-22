# BaalSTT — autonomous Speech-to-Text API

Audio rein → Text raus. Powered by **Deepgram nova-2**. Self-hosted, crypto-paid, no signup.

**Live:** <https://chrispc.tailb3821a.ts.net/>

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Landing + browser demo (upload a file) |
| `/health` | GET | Liveness + free-tier state |
| `/transcribe?model=nova-2` | POST | Multipart `file=@audio.mp3` **or** JSON `{"url": "https://…"}` |
| `/verify` | POST | `{"txid": "<txid>", "chain": "auto"}` — after you pay, get a 72 h API key |

Models: `nova-2`, `whisper-large-v3`, `whisper-large-v3-turbo`.

## Pricing
- **Free:** 3 transcriptions / 24 h / IP
- **Beyond:** pay-what-you-want ≥ ~2 USD in **BTC** or **SOL** (addresses returned
  with the HTTP 402). Then `POST /verify` with your txid — the API checks the
  real chain (Blockstream / Solana-RPC) and returns a key that lifts the cap:

```bash
curl -F "file=@meeting.wav" "https://chrispc.tailb3821a.ts.net/transcribe?model=nova-2"

# after paying:
curl -X POST https://chrispc.tailb3821a.ts.net/verify \
     -H "Content-Type: application/json" \
     -d '{"txid": "…", "chain": "auto"}'
# → {"ok": true, "api_key": "baal_…"}

# paid usage:
curl -F "file=@big.wav" -H "X-BaalSTT-Key: baal_…" \
     "https://chrispc.tailb3821a.ts.net/transcribe"
```

## Security
- SSRF guard on the URL path (private/Tailscale/metadata ranges blocked)
- Global daily cap (150 transcriptions/24 h) as credit-burn guard
- Payments verified on-chain against our wallet addresses — no trust needed

## Self-host
```bash
pip install fastapi uvicorn python-multipart
export BAALSTT_DG_KEY=…      # Deepgram API key (or set BAALSTT_IDENTITY=<path>)
python baalstt.py
```
Optional env: `BAALSTT_BTC`, `BAALSTT_SOL` (payment addresses).

## Run
```python
uvicorn baalstt:app --host 0.0.0.0 --port 8090
```

MIT © 2026 BAAL
