# Local Library

Ask questions about anything in Wikipedia from any phone or computer on your network. It's answered by a small AI model
running on your own machine, using an offline copy of Wikipedia, with links to the local articles it used.

```
phone/computer ──► chat page :80 ──► kiwix-serve (offline Wikipedia, full-text search)
                        │       └──► Jev re-ranking (optional, TypeSafe API)
                        └──────────► Ollama (qwen3.5:4b) writes the answer from the passages it was given
```

1. The model turns your message into a standalone question plus search keywords.
2. kiwix-serve full-text searches Wikipedia; the top results become candidates.
3. The best 3 articles are picked (by Jev if enabled, otherwise by search rank), and the most relevant paragraphs
   from them are selected (by Jev, otherwise by keyword overlap).
4. The model answers only from those paragraphs and cites them as [1], [2]… linking to the local article.

## Install (Ubuntu Server 24.04+)

```bash
git clone https://github.com/JonTelep/local-library.git
cd local-library
sudo ./install.sh
```

Then open `http://<server-ip>/` on your phone. The installer prints the address.

What it sets up:
- NVIDIA driver (if an NVIDIA GPU is found; reboot once afterward)
- Ollama plus the model, reachable on the LAN at `:11434` with the model kept loaded
- kiwix-serve plus the latest English Wikipedia, checksum-verified; the download resumes if you re-run after an interruption
- The chat page as a systemd service on port 80
- Closing the laptop lid no longer suspends it

**Options** (environment variables):

| Variable | Default | |
|---|---|---|
| `VARIANT` | `maxi` | `maxi` ≈ 119 GB with images, `nopic` ≈ 50 GB, `mini` ≈ 11 GB (article intros only) |
| `MODEL` | `qwen3.5:4b` | Any Ollama model. On a 6 GB GPU, stay at 8B or below |
| `TYPESAFE_API_KEY` | none | Turns on Jev re-ranking |
| `PORT` | `80` | Chat page port |

```bash
sudo TYPESAFE_API_KEY=<your-key> ./install.sh
```

Settings live in `/etc/local-library.env`; after editing, run `sudo systemctl restart local-library`.

**Update Wikipedia:** run `git pull && sudo ./install.sh` again. It downloads the newest edition and deletes the old one.

## Jev (optional)

Picking the right article is where a 4B model struggles most. With `TYPESAFE_API_KEY` set, [Jev](https://typesafe.ai)
scores every search result and every paragraph of the top articles for "does this help answer the question?", and the
best ones go to the local model. Your questions and the candidate text are then sent to TypeSafe's API; everything else
stays local. If the API can't be reached, it falls back to the offline ranking automatically.

## Notes

- There's no login: anyone on your network can use the chat page and Ollama. Don't forward these ports to the internet.
- Logs: `journalctl -u local-library -u kiwix -u ollama -f`
- Test: `python3 test_app.py`
