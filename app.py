# -*- coding: utf-8 -*-
"""Локальная программа: фон фото становится белым, сам кадр не меняется."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

WEB = os.environ.get("WEB") == "1" or bool(os.environ.get("RAILWAY_ENVIRONMENT"))
PORT = int(os.environ.get("PORT", "8765"))
MODEL = "u2netp" if WEB else "isnet-general-use"
HOST = "0.0.0.0" if WEB else "127.0.0.1"
EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

session = None
model_error = ""
lock = threading.Lock()
outputs = {}
state = {
    "phase": "model",  # model | idle | run | done | error
    "done": 0,
    "total": 0,
    "current": "",
    "message": "Готовлю модель",
    "out_dir": "",
    "errors": [],
    "preview": 0,
}

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)


def providers():
    import onnxruntime as ort

    available = ort.get_available_providers()
    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if "DmlExecutionProvider" in available:
        return ["DmlExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def open_session(force_cpu=False):
    from rembg import new_session

    prov = ["CPUExecutionProvider"] if force_cpu else providers()
    return new_session(MODEL, providers=prov)


def load_model():
    global session, model_error
    try:
        try:
            session = open_session(False)
        except Exception:
            session = open_session(True)
        with lock:
            if state["phase"] == "model":
                state["phase"] = "idle"
                state["message"] = "Можно грузить фото"
    except Exception as e:
        model_error = str(e)
        with lock:
            state["phase"] = "error"
            state["message"] = "Модель не загрузилась: " + model_error


def cpu_fallback():
    global session
    session = open_session(True)


def list_images(folder, out_dir):
    out_dir = os.path.abspath(out_dir)
    found = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in EXT:
            continue
        if os.path.abspath(path).startswith(out_dir + os.sep):
            continue
        found.append(path)
    return found


def cutout(rgb):
    from rembg import remove

    try:
        return remove(rgb, session=session)
    except Exception:
        cpu_fallback()
        return remove(rgb, session=session)


def to_white(im):
    im = ImageOps.exif_transpose(im)
    icc = im.info.get("icc_profile")
    rgb = im.convert("RGB")
    cut = cutout(rgb)
    alpha = cut.getchannel("A")
    hist = alpha.histogram()
    if sum(hist[16:]) / float(rgb.size[0] * rgb.size[1]) < 0.01:
        raise RuntimeError("не нашёл объект, файл пропущен")

    # Белый только там, где фон. Непрозрачные пиксели человека остаются исходными.
    out = Image.new("RGB", rgb.size, (255, 255, 255))
    out.paste(rgb, mask=alpha)
    if icc:
        out.info["icc_profile"] = icc
    return out


def process_upload(data, name):
    while session is None and not model_error:
        time.sleep(0.2)
    if session is None:
        raise RuntimeError(model_error or "модель не загрузилась")
    im = Image.open(io.BytesIO(data))
    im.load()
    im = ImageOps.exif_transpose(im)
    w, h = im.size
    long_side = max(w, h)
    if long_side > 1600:
        scale = 1600 / long_side
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    print("process", name, im.size, flush=True)
    result = to_white(im)
    bio = io.BytesIO()
    icc = result.info.get("icc_profile")
    params = {"quality": 95, "subsampling": 0, "optimize": True}
    if icc:
        params["icc_profile"] = icc
    result.save(bio, "JPEG", **params)
    raw = bio.getvalue()
    stem = os.path.splitext(os.path.basename(name or "photo.jpg"))[0]
    stem = "".join(c for c in stem if c.isalnum() or c in "._- ")[:80] or "photo"
    rid = uuid.uuid4().hex[:12]
    with lock:
        outputs[rid] = (stem + ".jpg", raw)
        while len(outputs) > 40:
            outputs.pop(next(iter(outputs)))
    return rid, raw


def save_jpg(im, path):
    icc = im.info.get("icc_profile")
    params = {"quality": 95, "subsampling": 0, "optimize": True}
    if icc:
        params["icc_profile"] = icc
    im.save(path, "JPEG", **params)


def preview(src, out):
    def fit(im):
        im = im.copy()
        im.thumbnail((900, 900))
        return im

    fit(src).save(os.path.join(CACHE, "in.jpg"), "JPEG", quality=85)
    fit(out).save(os.path.join(CACHE, "out.jpg"), "JPEG", quality=85)


def run_job(files, out_dir):
    while session is None and not model_error:
        time.sleep(0.2)
    if session is None:
        with lock:
            state["phase"] = "error"
            state["message"] = "Модель не загрузилась"
        return
    os.makedirs(out_dir, exist_ok=True)
    sources = {os.path.abspath(p) for p in files}
    errors = []
    with lock:
        state["phase"] = "run"
        state["done"] = 0
        state["total"] = len(files)
        state["errors"] = []
        state["out_dir"] = out_dir
        state["message"] = "Делаю белый фон"
    for i, path in enumerate(files, 1):
        name = os.path.basename(path)
        with lock:
            state["current"] = name
            state["message"] = f"{i} / {len(files)}  {name}"
        try:
            im = Image.open(path)
            im.load()
            result = to_white(im)
            stem = os.path.splitext(name)[0]
            dest = os.path.join(out_dir, stem + ".jpg")
            if os.path.abspath(dest) in sources:
                dest = os.path.join(out_dir, stem + " белый.jpg")
            save_jpg(result, dest)
            preview(ImageOps.exif_transpose(im).convert("RGB"), result)
            with lock:
                state["preview"] = i
        except Exception as e:
            errors.append(f"{name}: {e}")
        with lock:
            state["done"] = i
            state["errors"] = errors
    with lock:
        state["phase"] = "done"
        state["current"] = ""
        if errors:
            state["message"] = f"Готово. Не вышло: {len(errors)}"
        else:
            state["message"] = f"Готово, {len(files)} фото"


def ps_pick(kind):
    dest = os.path.join(tempfile.gettempdir(), f"whitetbg-{threading.get_ident()}.txt")
    if os.path.exists(dest):
        os.remove(dest)
    dest_ps = dest.replace("'", "''")
    if kind == "files":
        body = r"""
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Multiselect = $true
$d.Title = 'Фото'
$d.Filter = 'Фото|*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff'
[void]$d.ShowDialog($f)
$text = $d.FileNames -join '|'
"""
    else:
        body = r"""
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Папка'
[void]$d.ShowDialog($f)
$text = $d.SelectedPath
"""
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.TopMost = $true
$f.ShowInTaskbar = $false
$f.Opacity = 0
$f.Show()
{body}
$f.Close()
[System.IO.File]::WriteAllText('{dest_ps}', $text, (New-Object System.Text.UTF8Encoding $false))
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        check=False,
    )
    if not os.path.exists(dest):
        return ""
    with open(dest, "r", encoding="utf-8") as f:
        text = f.read().strip()
    try:
        os.remove(dest)
    except OSError:
        pass
    return text


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Белый фон</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #f3f3f3; color: #111; font: 15px/1.4 "Segoe UI", sans-serif; }
  main { max-width: 760px; margin: 36px auto; padding: 0 16px 40px; }
  h1 { font-size: 28px; font-weight: 650; margin: 0 0 6px; }
  .lead { margin: 0 0 22px; color: #333; }
  label { display: block; margin: 14px 0 6px; font-size: 13px; color: #333; }
  .row { display: flex; gap: 8px; }
  input[type=text] { flex: 1; min-width: 0; padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px; font: inherit; background: #fff; }
  button { padding: 10px 14px; border-radius: 8px; border: 1px solid #111; background: #fff; color: #111; font: inherit; cursor: pointer; }
  button.primary { background: #111; color: #fff; width: 100%; margin-top: 18px; padding: 13px; font-size: 16px; }
  button:disabled { opacity: .45; cursor: default; }
  .bar { height: 8px; background: #e4e4e4; border-radius: 99px; margin-top: 16px; overflow: hidden; }
  .bar > div { height: 100%; width: 0; background: #111; }
  .status { margin-top: 8px; min-height: 1.3em; font-size: 13px; }
  .err { color: #9a1b1b; white-space: pre-wrap; font-size: 13px; }
  .preview { margin-top: 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .preview figure { margin: 0; }
  .preview img { width: 100%; display: block; background: #ddd; border-radius: 8px; }
  .preview figcaption { font-size: 12px; color: #555; margin-top: 4px; }
  a.link { color: #111; }
  .drop { margin-top: 8px; border: 1.5px dashed #bbb; border-radius: 12px; padding: 36px 16px; text-align: center; background: #fff; color: #111; cursor: pointer; font-size: 18px; }
  .drop.over { border-color: #111; color: #111; }
  .gallery { margin-top: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .gallery a { display: block; }
  .gallery img { width: 100%; border-radius: 8px; background: #ddd; }
</style>
</head>
<body>
<main>
  <h1>Белый фон</h1>
  <p class="lead">Фон становится белым. Лицо, одежда и цвет остаются как на фото.</p>

  <div id="web" hidden>
    <input id="file" type="file" accept="image/*" multiple hidden>
    <div id="drop" class="drop">Нажми и выбери фото</div>
    <button id="pickWeb" type="button" class="primary">Выбрать фото</button>
    <button id="goWeb" class="primary" type="button">Сделать белый фон</button>
    <div class="gallery" id="gallery"></div>
    <a id="zip" class="link" hidden href="/api/zip">Скачать все</a>
  </div>

  <div id="local">
  <label>Откуда</label>
  <div class="row">
    <input id="src" type="text" placeholder="Папка с фото" spellcheck="false">
    <button id="pickSrc" type="button">Папка</button>
    <button id="pickFiles" type="button">Фото</button>
  </div>

  <label>Куда сохранить</label>
  <div class="row">
    <input id="dst" type="text" placeholder="Папка для готовых" spellcheck="false">
    <button id="pickDst" type="button">Папка</button>
  </div>

  <button id="go" class="primary" type="button">Сделать белый фон</button>
  </div>

  <div class="bar"><div id="bar"></div></div>
  <div class="status" id="status">Загрузка…</div>
  <div class="err" id="err"></div>

  <div class="preview" id="preview" hidden>
    <figure>
      <img id="imgIn" alt="">
      <figcaption>Было</figcaption>
    </figure>
    <figure>
      <img id="imgOut" alt="">
      <figcaption>Стало</figcaption>
    </figure>
  </div>
</main>
<script>
const src = document.getElementById("src");
const dst = document.getElementById("dst");
const statusEl = document.getElementById("status");
const errEl = document.getElementById("err");
const bar = document.getElementById("bar");
const go = document.getElementById("go");
const preview = document.getElementById("preview");
const WEB = __WEB__;
let files = [];
let picked = [];
let lastPreview = 0;
let made = [];
let busyWeb = false;

if (WEB) {
  document.getElementById("web").hidden = false;
  document.getElementById("local").hidden = true;
  document.getElementById("preview").hidden = true;
}

function setBusy(on) {
  go.disabled = on;
  document.getElementById("pickSrc").disabled = on;
  document.getElementById("pickFiles").disabled = on;
  document.getElementById("pickDst").disabled = on;
}

async function pick(kind) {
  setBusy(true);
  statusEl.textContent = "Окно выбора открыто";
  const r = await fetch("/api/pick", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({kind})
  });
  const data = await r.json();
  setBusy(false);
  if (kind === "files") {
    files = data.paths || [];
    src.value = files.length ? ("Фото: " + files.length) : "";
    if (files.length && !dst.value) {
      const folder = files[0].replace(/\\\\[^\\\\]+$/, "");
      dst.value = folder + "\\\\белый фон";
    }
  } else if (data.path) {
    if (kind === "src") {
      files = [];
      src.value = data.path;
      if (!dst.value) dst.value = data.path.replace(/[\\\\/]+$/, "") + "\\\\белый фон";
    } else {
      dst.value = data.path;
    }
  }
  poll();
}

document.getElementById("pickSrc").onclick = () => pick("src");
document.getElementById("pickDst").onclick = () => pick("dst");
document.getElementById("pickFiles").onclick = () => pick("files");

go.onclick = async () => {
  errEl.textContent = "";
  const body = {output: dst.value, files: files};
  if (!files.length) body.input = src.value;
  const r = await fetch("/api/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  const data = await r.json();
  if (!r.ok) errEl.textContent = data.error || "Не вышло";
  poll();
};

let webNote = "";
async function poll() {
  const s = await fetch("/api/status").then(r => r.json());
  const pct = s.total ? Math.round(100 * s.done / s.total) : (s.phase === "done" ? 100 : 0);
  bar.style.width = pct + "%";
  const busy = s.phase === "run" || s.phase === "model";
  setBusy(busy);
  if (s.phase === "model") go.disabled = true;
  if (WEB) {
    document.getElementById("goWeb").disabled = s.phase === "model" || busyWeb;
    if (webNote) statusEl.textContent = webNote;
    else if (s.phase === "model") statusEl.textContent = "Готовлю, подожди";
    else if (picked.length) statusEl.textContent = "Выбрано: " + picked.length + ". Жми «Сделать белый фон»";
    else statusEl.textContent = "Нажми «Выбрать фото»";
  } else {
    statusEl.textContent = s.message || "";
  }
  errEl.textContent = (s.errors || []).join("\\n");
  if (s.preview && s.preview !== lastPreview) {
    lastPreview = s.preview;
    preview.hidden = false;
    const t = Date.now();
    document.getElementById("imgIn").src = "/cache/in.jpg?t=" + t;
    document.getElementById("imgOut").src = "/cache/out.jpg?t=" + t;
  }
  if (s.phase === "done" && s.out_dir) {
    statusEl.innerHTML = s.message + " — <a class='link' id='open' href='#'>открыть папку</a>";
    document.getElementById("open").onclick = (e) => {
      e.preventDefault();
      fetch("/api/open", {method: "POST"});
    };
  }
}
function addPicked(list) {
  picked = [...list];
  webNote = "";
  document.getElementById("drop").textContent = picked.length ? ("Выбрано фото: " + picked.length) : "Нажми и выбери фото";
}
const drop = document.getElementById("drop");
document.getElementById("pickWeb").onclick = () => filePick();
drop.onclick = () => filePick();
function filePick() { document.getElementById("file").click(); }
document.getElementById("file").onchange = (e) => addPicked(e.target.files);
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); addPicked(e.dataTransfer.files); };

document.getElementById("goWeb").onclick = async () => {
  if (!picked.length) { webNote = "Сначала нажми «Выбрать фото»"; statusEl.textContent = webNote; return; }
  errEl.textContent = "";
  made = [];
  busyWeb = true;
  document.getElementById("gallery").innerHTML = "";
  document.getElementById("zip").hidden = true;
  document.getElementById("goWeb").disabled = true;
  try {
    for (let i = 0; i < picked.length; i++) {
      webNote = "Делаю " + (i + 1) + " из " + picked.length + ". Не закрывай страницу";
      statusEl.textContent = webNote;
      bar.style.width = Math.round(100 * i / picked.length) + "%";
      const r = await fetch("/api/process?name=" + encodeURIComponent(picked[i].name), {
        method: "POST",
        headers: {"Content-Type": "application/octet-stream"},
        body: picked[i]
      });
      if (!r.ok) {
        webNote = "Не вышло: " + picked[i].name;
        statusEl.textContent = webNote;
        continue;
      }
      const id = r.headers.get("X-Id");
      made.push(id);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const dot = picked[i].name.lastIndexOf(".");
      a.download = (dot > 0 ? picked[i].name.slice(0, dot) : picked[i].name) + ".jpg";
      const img = document.createElement("img");
      img.src = url;
      a.appendChild(img);
      document.getElementById("gallery").appendChild(a);
    }
  } catch (e) {
    webNote = "Связь оборвалась, попробуй ещё раз";
    statusEl.textContent = webNote;
  }
  bar.style.width = "100%";
  busyWeb = false;
  document.getElementById("goWeb").disabled = false;
  if (made.length) {
    webNote = "Готово. Фото ниже — нажми на него, скачается";
    statusEl.textContent = webNote;
    const zip = document.getElementById("zip");
    zip.href = "/api/zip?ids=" + made.join(",");
    zip.hidden = false;
    zip.textContent = "Скачать все";
    document.getElementById("gallery").scrollIntoView({behavior: "smooth", block: "start"});
  }
};

setInterval(poll, 700);
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            page = HTML.replace("__WEB__", "true" if WEB else "false")
            if WEB:
                page = page.replace('<div id="web" hidden>', '<div id="web">', 1)
                page = page.replace('<div id="local">', '<div id="local" hidden>', 1)
            raw = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in ("/cache/in.jpg", "/cache/out.jpg"):
            fp = os.path.join(CACHE, os.path.basename(path))
            if not os.path.isfile(fp):
                self.send_error(404)
                return
            data = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/status":
            with lock:
                self._json(200, dict(state))
            return
        if path == "/api/zip":
            ids = parse_qs(urlparse(self.path).query).get("ids", [""])[0].split(",")
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
                for rid in ids:
                    item = outputs.get(rid)
                    if item:
                        zf.writestr(item[0], item[1])
            raw = bio.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=belyy-fon.zip")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/process":
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > 40 * 1024 * 1024:
                self._json(400, {"error": "пустое или слишком большое фото"})
                return
            data = self.rfile.read(n)
            name = parse_qs(urlparse(self.path).query).get("name", ["photo.jpg"])[0]
            try:
                rid, raw = process_upload(data, name)
            except Exception as e:
                msg = str(e).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("X-Id", rid)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/pick":
            body = self._read_json()
            kind = body.get("kind")
            if kind == "files":
                text = ps_pick("files")
                paths = [p for p in text.split("|") if p and os.path.isfile(p)]
                self._json(200, {"paths": paths})
                return
            if kind in ("src", "dst"):
                text = ps_pick("folder")
                self._json(200, {"path": text if os.path.isdir(text) else ""})
                return
            self._json(400, {"error": "не понял выбор"})
            return

        if path == "/api/open":
            with lock:
                folder = state.get("out_dir") or ""
            if folder and os.path.isdir(folder):
                os.startfile(folder)
            self._json(200, {"ok": True})
            return

        if path == "/api/start":
            body = self._read_json()
            with lock:
                if state["phase"] in ("run", "model"):
                    self._json(409, {"error": "Подожди, ещё занято"})
                    return
            files = body.get("files") or []
            out_dir = (body.get("output") or "").strip().strip('"')
            if files:
                files = [p for p in files if os.path.isfile(p)]
                if not out_dir and files:
                    out_dir = os.path.join(os.path.dirname(files[0]), "белый фон")
            else:
                folder = (body.get("input") or "").strip().strip('"')
                if not os.path.isdir(folder):
                    self._json(400, {"error": "Папка с фото не найдена"})
                    return
                if not out_dir:
                    out_dir = os.path.join(folder, "белый фон")
                files = list_images(folder, out_dir)
            if not files:
                self._json(400, {"error": "В папке нет фото"})
                return
            threading.Thread(target=run_job, args=(files, out_dir), daemon=True).start()
            self._json(200, {"ok": True, "total": len(files)})
            return

        self.send_error(404)


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        if not WEB:
            webbrowser.open(url)
        print("Уже открыто:", url)
        sys.exit(0)
    threading.Thread(target=load_model, daemon=True).start()
    if not WEB:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    print(url)
    print("Окно не закрывай, пока пользуешься.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
