# intelli-scaper (Web Crawler)

A lightweight, browser-controlled **web crawler** built with **Flask** and **Playwright**.  
Start crawls, monitor live logs, and download results – all from a sleek Apple-like UI.  

![App UI](https://github.com/user-attachments/assets/312e3b1b-c1aa-4097-ab48-2b40b68f06d1)

## ✨ Features
- **Browser UI** – Start/stop crawls, configure options, and view real-time logs  
- **Multi-Worker Engine** – Parallel Playwright workers for efficient crawling  
- **Customizable Options** – Domain, start path, allowed prefixes, quick/stealth modes, concurrency, request delay, etc.  
- **Memory-Safe Output** – Automatic flush to disk (`output/<domain>/pages.ndjson` + `pages.json`)  
- **Downloadable Results** – Per-domain results accessible via the UI 

## 🛠️ Tech
- **Backend**: Flask (Python)  
- **Crawler**: Playwright (Chromium), `html2text`  
- **UI**: TailwindCSS, Apple-inspired design  

## 📂 Output
Results are stored in `output/<domain>/`:
- `pages.ndjson` – newline-delimited JSON  
- `pages.json` – rolling snapshot  


A modular Playwright-based crawler wrapped in a Flask server with an “Apple-like” UI:

- Start/stop crawls from the browser
- Live terminal-style logs
- Periodic flush-to-disk to keep memory low
- Results saved under `output/<domain>/`
- Browse and download output files from the UI
- All options are exposed in the UI and via REST

> Everything runs through **`server.py`**. Server listens on **http://127.0.0.1:5000**.

---

## Quick Start

1) **Create and activate a virtual environment (recommended)**

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows (PowerShell)
# .venv\Scripts\Activate.ps1


pip install -r requirements.txt
python -m playwright install

python server.py
```