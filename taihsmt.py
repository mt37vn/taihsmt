import os, time, re, json, random, zipfile, threading, subprocess, ssl
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests as req_lib
from requests.adapters import HTTPAdapter
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="VNeGP Downloader")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vnegp_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

AGENT_BASE_URL = "http://localhost:1234"
TBMT_CACHE_FILE = os.path.join(CACHE_DIR, "_tbmt_index.json")

_progress_store = {}
_scanner_lock = threading.Lock()
_last_scan_time = 0.0
MIN_SCAN_INTERVAL = 30

XHR_INTERCEPT_JS = """
window.__xhrResponses = window.__xhrResponses || [];
if (!window.__xhrHooked) {
    window.__xhrHooked = true;
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;
    var _origSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__url = url; this.__method = method; this.__headers = {};
        return _origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        this.__headers[name] = value;
        return _origSetRequestHeader.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        this.addEventListener('load', function() {
            try { window.__xhrResponses.push({url: self.__url, method: self.__method, status: self.status, headers: self.__headers, body: self.responseText.substring(0, 200000)}); } catch(e) {}
        });
        return _origSend.apply(this, arguments);
    };
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        return _origFetch.apply(this, arguments).then(function(resp) {
            var clone = resp.clone();
            clone.text().then(function(txt) {
                window.__xhrResponses.push({url: typeof url === 'string' ? url : url.url, method: (opts && opts.method) || 'GET', status: clone.status, headers: opts && opts.headers || {}, body: txt.substring(0, 200000)});
            }).catch(function(){});
            return resp;
        });
    };
}
"""


PORTAL_BASE = "https://muasamcong.mpi.gov.vn"
HSMT_API_PATH = "/o/egp-portal-contractor-selection-v2/services/lcnt_tbmt_hsmt"


class _LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _extract_hsmt_params(driver):
    from urllib.parse import urlparse, parse_qs
    current_url = driver.current_url
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query)
    notify_id = (qs.get("notifyId", [None]) or [None])[0]
    process_apply = (qs.get("processApply", ["LDT"]) or ["LDT"])[0]
    if not notify_id:
        raise Exception("Cannot extract notifyId from page URL")
    auth_token = driver.execute_script("return Liferay.authToken;")
    if not auth_token:
        raise Exception("Cannot extract Liferay.authToken from page")
    cookies = driver.get_cookies()
    cookie_dict = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
    return notify_id, process_apply, auth_token, cookie_dict


def _call_hsmt_api_direct(notify_id, process_apply, auth_token, cookies):
    api_url = f"{PORTAL_BASE}{HSMT_API_PATH}?token={auth_token}"
    payload = {"id": notify_id, "processApply": process_apply}
    timeout_sec = 30
    session = req_lib.Session()
    session.mount("https://", _LegacySSLAdapter())
    resp = session.post(
        api_url,
        json=payload,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": PORTAL_BASE,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0.0.0",
        },
        cookies=cookies,
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        raise Exception(f"HSMT API returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _is_valid_uuid(val):
    return bool(_UUID_RE.match(str(val))) if val else False


def _parse_hsmt_formvalue(response_json):
    file_items = []
    bido_list = response_json.get("bidoInvBiddingDTO") or []
    for dto in bido_list:
        fv_str = dto.get("formValue")
        if not fv_str:
            continue
        try:
            fv = json.loads(fv_str) if isinstance(fv_str, str) else fv_str
        except (json.JSONDecodeError, TypeError):
            continue

        def find_file_ids(obj):
            results = []
            if isinstance(obj, dict):
                if (
                    "fileId" in obj and "fileName" in obj
                    and _is_valid_uuid(obj["fileId"])
                    and obj.get("fileName")
                ):
                    results.append({"fileId": obj["fileId"], "fileName": obj["fileName"]})
                for v in obj.values():
                    results.extend(find_file_ids(v))
            elif isinstance(obj, list):
                for v in obj:
                    results.extend(find_file_ids(v))
            return results

        found = find_file_ids(fv)
        for fi in found:
            if fi["fileId"] not in {f.get("fileId") for f in file_items}:
                file_items.append(fi)
    return file_items


def _sprint(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode())


def _load_tbmt_index():
    if os.path.exists(TBMT_CACHE_FILE):
        try:
            with open(TBMT_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_tbmt_index(idx):
    with open(TBMT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def _find_chrome_exe():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in paths:
        if os.path.isfile(p):
            return p
    import shutil as _shutil
    found = _shutil.which("chrome")
    if found:
        return found
    return None


def _user_data_dir():
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    if os.path.isdir(default):
        return default
    return None


def _ensure_chrome_debugger(port=9222):
    debugger_url = f"http://localhost:{port}"
    try:
        resp = req_lib.get(f"{debugger_url}/json/version", timeout=3)
        if resp.status_code == 200:
            _sprint(f"Chrome debugger already listening on port {port}")
            return True
    except:
        pass

    chrome_exe = _find_chrome_exe()
    if not chrome_exe:
        raise Exception(
            "Chrome not found. Start Chrome manually with: "
            f'chrome.exe --remote-debugging-port={port}'
        )

    udd = _user_data_dir()
    if not udd:
        raise Exception(
            "Cannot determine Chrome user data directory. "
            f"Start Chrome manually with: chrome.exe --remote-debugging-port={port}"
        )

    _sprint(f"Restarting Chrome with debugger on port {port} (user data: {udd})...")
    import shutil as _shutil
    chrome_kill = _shutil.which("taskkill")
    if chrome_kill:
        subprocess.run(
            [chrome_kill, "/F", "/IM", "chrome.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    time.sleep(3)

    subprocess.Popen(
        [chrome_exe, f"--remote-debugging-port={port}", f"--user-data-dir={udd}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            resp = req_lib.get(f"{debugger_url}/json/version", timeout=2)
            if resp.status_code == 200:
                _sprint("Chrome debugger ready")
                return False
        except:
            pass
        time.sleep(1)
    raise Exception(f"Chrome did not start debugging on port {port} within 20s")


def find_bid_detail_url(driver, tbmt_code):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    driver.get("https://muasamcong.mpi.gov.vn/web/guest/contractor-selection?render=index")
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    try:
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='keyword'], input.form-control, input[type='text']")))
    except:
        time.sleep(5)

    search_input = None
    for sel in ["input[name='keyword']", "input.form-control", "input[type='text']"]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    search_input = el
                    break
            if search_input:
                break
        except:
            continue
    if not search_input:
        try:
            el = driver.find_element(By.XPATH, "//input")
            if el and el.is_displayed():
                search_input = el
        except:
            pass
    if not search_input:
        raise Exception("Cannot find search input")

    search_input.click()
    time.sleep(0.5)
    search_input.clear()
    time.sleep(0.3)
    for ch in tbmt_code:
        search_input.send_keys(ch)
        time.sleep(random.uniform(0.15, 0.35))
    time.sleep(1.5)
    search_input.send_keys(Keys.ENTER)
    time.sleep(5)

    result_link = None
    for sel in [
        f"a[href*='notifyNo={tbmt_code}']",
        f"a[href*='{tbmt_code}']",
        "a[href*='detail-v2']",
        "a.tbmt-title",
    ]:
        try:
            for a in driver.find_elements(By.CSS_SELECTOR, sel):
                if tbmt_code in (a.get_attribute("href") or "") or tbmt_code in a.text:
                    result_link = a
                    break
            if result_link:
                break
        except:
            continue
    if not result_link:
        for a in driver.find_elements(By.XPATH, "//a[contains(@href, 'contractor-selection')]"):
            if tbmt_code in (a.get_attribute("href") or "") or tbmt_code in a.text:
                result_link = a
                break
    if not result_link:
        raise Exception(f"No result found for {tbmt_code}")

    driver.execute_script("arguments[0].scrollIntoView(true);", result_link)
    time.sleep(2)
    driver.execute_script("arguments[0].click();", result_link)
    time.sleep(6)
    return driver.current_url


def scan_and_download_tbmt(tbmt_code: str, detail_url: str = None):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    import shutil

    tbmt_dir = os.path.join(CACHE_DIR, f".tmp_{tbmt_code}")
    os.makedirs(tbmt_dir, exist_ok=True)

    _ensure_chrome_debugger()

    opts = Options()
    opts.add_experimental_option("debuggerAddress", "localhost:9222")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.switch_to.new_window('tab')

    try:
        if detail_url:
            _sprint(f"1. Navigating directly to detail page for {tbmt_code}...")
            driver.get(detail_url)
            try:
                WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".nav-tabs, [class*='tab'], .portlet-body")))
            except:
                time.sleep(8)
        else:
            _sprint(f"1. Searching and navigating to detail page for {tbmt_code}...")
            url = find_bid_detail_url(driver, tbmt_code)
            _sprint(f"   URL: {url.encode('ascii','replace').decode()}")

        _sprint("2. Extracting HSMT API params and calling API directly...")
        file_items = []
        direct_ok = False
        try:
            notify_id, process_apply, auth_token, cookies = _extract_hsmt_params(driver)
            hsmt_response = _call_hsmt_api_direct(notify_id, process_apply, auth_token, cookies)
            file_items = _parse_hsmt_formvalue(hsmt_response)
            direct_ok = True
            _sprint(f"   Direct API: {len(file_items)} file items extracted")
        except Exception as e:
            _sprint(f"   Direct API attempt failed: {e}")
            _sprint("   Falling back to XHR interception path...")

        if not direct_ok:
            _sprint("3. Injecting XHR interceptor...")
            driver.execute_script(XHR_INTERCEPT_JS)

            _sprint("4. Waiting for initial page load...")
            time.sleep(8)

            _sprint("5. Finding 'Hồ sơ mời thầu' tab...")
            hsmt_tab_clicked = False
            for el in driver.find_elements(By.CSS_SELECTOR, ".nav-tabs a, ul.nav a, [class*='tab'] a"):
                text = (el.text or "").strip()
                href = (el.get_attribute("href") or "").lower()
                if "hồ sơ mời thầu" in text.lower() or "hs-mt" in href or "hsmot" in href:
                    driver.execute_script("arguments[0].click();", el)
                    _sprint(f"   Clicked tab: {text}")
                    hsmt_tab_clicked = True
                    time.sleep(4)
                    break
            if not hsmt_tab_clicked:
                for tab_sel in ["a[href*='tender-invitation']", "a[href*='notify-contractor-step-1-tbmt']", ".nav-tabs a:nth-child(2)"]:
                    try:
                        for el in driver.find_elements(By.CSS_SELECTOR, tab_sel):
                            if el.is_displayed():
                                driver.execute_script("arguments[0].click();", el)
                                _sprint(f"   Fallback tab: {tab_sel}")
                                time.sleep(4)
                                break
                    except:
                        continue

            _sprint("6. Waiting for XHR data to arrive...")
            time.sleep(8)

            _sprint("7. Capturing XHR responses and extracting file IDs from formValue...")
            xhr_raw = driver.execute_script("return JSON.stringify(window.__xhrResponses || [])")
            xhr_data = json.loads(xhr_raw) if xhr_raw else []

            if xhr_data:
                dump_path = os.path.join(CACHE_DIR, f".xhr_{tbmt_code}.json")
                with open(dump_path, "w", encoding="utf-8") as f:
                    json.dump([{"url": x.get("url",""), "method": x.get("method",""), "status": x.get("status"), "headers": x.get("headers",{}), "body": x.get("body","")} for x in xhr_data], f, ensure_ascii=False)

                for xr in xhr_data:
                    url = xr.get("url", "")
                    body = xr.get("body", "")
                    if "lcnt_tbmt_hsmt" in url or "lcnt" in url:
                        fv_matches = re.finditer(r'"[fF]orm[Vv]alue"\s*:\s*"((?:[^"\\]|\\[^u]|\\u[0-9a-fA-F]{4})*)"', body)
                        for m in fv_matches:
                            try:
                                fv_str = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
                                fv = json.loads(fv_str)
                                def find_file_ids(obj):
                                    results = []
                                    if isinstance(obj, dict):
                                        if (
                                            "fileId" in obj and "fileName" in obj
                                            and _is_valid_uuid(obj["fileId"])
                                            and obj.get("fileName")
                                        ):
                                            results.append({"fileId": obj["fileId"], "fileName": obj["fileName"]})
                                        for v in obj.values():
                                            results.extend(find_file_ids(v))
                                    elif isinstance(obj, list):
                                        for v in obj:
                                            results.extend(find_file_ids(v))
                                    return results
                                found = find_file_ids(fv)
                                for fi in found:
                                    if fi["fileId"] not in {f.get("fileId") for f in file_items}:
                                        file_items.append(fi)
                            except:
                                pass

            _sprint(f"   Captured {len(xhr_data)} XHR responses, {len(file_items)} file items")

        if not file_items:
            raise Exception(f"No file IDs found in HSMT response for {tbmt_code}")

        _sprint(">>> Calling agent bulk download API...")
        map_files = {fi["fileId"]: fi["fileName"] for fi in file_items}
        _sprint(f"   mapFiles: {json.dumps(map_files, ensure_ascii=False)[:300]}")
        zip_filename = f"File_{tbmt_code}.zip"
        import urllib.request, urllib.parse, urllib.error
        params = urllib.parse.urlencode({
            "mapFiles": json.dumps(map_files, separators=(",", ":")),
            "fileName": zip_filename,
        })
        url = f"{AGENT_BASE_URL}/api/download/files/browser/public?{params}"
        req = urllib.request.Request(url, headers={
            "Origin": "https://muasamcong.mpi.gov.vn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0.0.0",
        })
        try:
            resp = urllib.request.urlopen(req, timeout=180)
            zip_data = resp.read()
            _sprint(f"   HTTP {resp.status} ({len(zip_data)} bytes)")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:200]
            raise Exception(f"Agent API error {e.code}: {err_body}")
        except Exception as e:
            raise Exception(f"Agent API request failed: {e}")

        if zip_data[:4] != b"PK\x03\x04":
            raise Exception(f"Agent returned non-ZIP data ({zip_data[:20]})")

        _sprint(">>> Saving ZIP file...")
        zip_path = os.path.join(CACHE_DIR, f"{tbmt_code}.zip")
        with open(zip_path, "wb") as f:
            f.write(zip_data)

        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            file_names = zf.namelist()

        results = []
        for fn in file_names:
            info = zf.getinfo(fn)
            results.append({"filename": fn, "path": zip_path, "size": info.file_size})
            _sprint(f"   + {fn.encode('ascii','replace').decode()} ({info.file_size} bytes)")

        idx = _load_tbmt_index()
        idx[tbmt_code] = {
            "files": [r["filename"] for r in results],
            "downloaded_at": datetime.now().isoformat(),
            "file_count": len(results),
        }
        _save_tbmt_index(idx)
        return results

    finally:
        try:
            driver.close()
            if len(driver.window_handles) > 0:
                driver.switch_to.window(driver.window_handles[0])
        except:
            pass
        try:
            shutil.rmtree(tbmt_dir, ignore_errors=True)
        except:
            pass


@app.post("/api/scan")
def api_scan_json(body: dict, background_tasks: BackgroundTasks):
    url = (body.get("url") or "").strip()
    tbmt_code = (body.get("tbmt_code") or "").strip()
    if not tbmt_code and not url:
        raise HTTPException(400, "Provide tbmt_code or url")
    if not tbmt_code:
        m = re.search(r'notifyNo=([A-Za-z0-9]+)', url)
        if m:
            tbmt_code = m.group(1)
        else:
            raise HTTPException(400, "Cannot extract TBMT code from URL")
    return _start_scan(tbmt_code, detail_url=url, background_tasks=background_tasks)


def _start_scan(code, background_tasks, detail_url=None):
    code = code.strip()
    if not code:
        raise HTTPException(400, "Missing TBMT code")

    idx = _load_tbmt_index()
    cached = idx.get(code, {})
    cached_files = [f for f in os.listdir(CACHE_DIR) if f.startswith(code) and not f.startswith(".tmp") and not f.startswith(".")]
    if cached and cached_files:
        return {
            "status": "cached",
            "tbmt_code": code,
            "files": cached.get("files", []),
            "downloaded_at": cached.get("downloaded_at"),
            "message": f"Already cached - use GET /api/files/{code}",
        }

    global _last_scan_time
    elapsed = time.time() - _last_scan_time
    if elapsed < MIN_SCAN_INTERVAL:
        remaining = int(MIN_SCAN_INTERVAL - elapsed)
        raise HTTPException(429, f"Rate limited. Wait {remaining}s (IP safety)")

    if _scanner_lock.locked():
        raise HTTPException(409, "Another scan in progress")

    _progress_store[code] = {"status": "scanning", "progress": "Starting...", "files": []}

    def _run():
        global _last_scan_time
        try:
            with _scanner_lock:
                _last_scan_time = time.time()
                _progress_store[code]["progress"] = "Navigating..."
                result = scan_and_download_tbmt(code, detail_url=detail_url)
                _progress_store[code] = {
                    "status": "done",
                    "progress": "Complete",
                    "files": [{"filename": r["filename"], "size": r["size"]} for r in result],
                }
        except Exception as e:
            _progress_store[code] = {"status": "error", "progress": str(e), "files": []}

    background_tasks.add_task(_run)
    return {"status": "started", "tbmt_code": code, "cooldown_sec": MIN_SCAN_INTERVAL}


@app.post("/api/scan/{tbmt_code}")
def api_scan(tbmt_code: str, background_tasks: BackgroundTasks):
    return _start_scan(tbmt_code, background_tasks=background_tasks)


@app.get("/api/scan/{tbmt_code}/status")
def api_scan_status(tbmt_code: str):
    code = tbmt_code.strip()
    info = _progress_store.get(code, {"status": "unknown", "progress": "", "files": []})
    idx = _load_tbmt_index()
    cached = idx.get(code, {})
    return {
        "tbmt_code": code,
        "status": info["status"],
        "progress": info["progress"],
        "files": info.get("files", []),
        "cached": bool(cached),
        "cached_files": cached.get("files", []),
    }


@app.get("/api/files/{tbmt_code}")
def api_files(tbmt_code: str):
    code = tbmt_code.strip()
    if not code:
        raise HTTPException(400, "Missing TBMT code")

    zip_path = os.path.join(CACHE_DIR, f"{code}.zip")
    if os.path.isfile(zip_path):
        return FileResponse(zip_path, filename=f"{code}.zip", media_type="application/zip")

    cached_files = [f for f in os.listdir(CACHE_DIR) if f.startswith(code) and not f.startswith(".tmp") and not f.startswith(".") and not f.endswith(".json") and not f.endswith(".zip")]

    if not cached_files:
        raise HTTPException(404, f"No files cached for {code}. Run POST /api/scan/{code} first.")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in cached_files:
            fp = os.path.join(CACHE_DIR, fn)
            if os.path.isfile(fp):
                zf.write(fp, fn)

    return FileResponse(zip_path, filename=f"{code}.zip", media_type="application/zip")


@app.get("/api/status")
def api_status():
    agent_ok = False
    try:
        r = req_lib.get(f"{AGENT_BASE_URL}/download/file/browser/public", timeout=5, verify=False)
        agent_ok = r.status_code in (200, 400, 404, 403)
    except:
        pass

    idx = _load_tbmt_index()
    cache_files = [f for f in os.listdir(CACHE_DIR) if not f.startswith(".") and not f.startswith(".tmp")]
    total_size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in cache_files if os.path.isfile(os.path.join(CACHE_DIR, f)) and not f.endswith(".json") and not f.endswith(".zip"))

    return {
        "agent": {"running": agent_ok, "url": AGENT_BASE_URL},
        "cache": {
            "directory": CACHE_DIR,
            "file_count": len([f for f in cache_files if not f.endswith(".json") and not f.endswith(".zip")]),
            "tbmt_codes": list(idx.keys()),
            "size_mb": round(total_size / (1024 * 1024), 2),
        },
        "scanner": {
            "busy": _scanner_lock.locked(),
            "cooldown_sec": max(0, int(MIN_SCAN_INTERVAL - (time.time() - _last_scan_time))),
            "min_interval": MIN_SCAN_INTERVAL,
        },
        "progress": {k: v["status"] for k, v in _progress_store.items()},
    }


@app.get("/api/cached-files")
def api_cached_files():
    files = []
    idx = _load_tbmt_index()
    for fn in os.listdir(CACHE_DIR):
        fp = os.path.join(CACHE_DIR, fn)
        if not os.path.isfile(fp) or fn.startswith(".") or fn.startswith(".tmp") or fn.endswith(".json"):
            continue
        s = os.stat(fp)
        tbmt_id = fn.replace(".zip", "")
        file_count = len(idx.get(tbmt_id, {}).get("files", [])) if fn.endswith(".zip") else 0
        files.append({
            "tbmt_id": tbmt_id,
            "filename": f"{tbmt_id}.zip ({file_count} files)" if fn.endswith(".zip") else fn,
            "size": f"{s.st_size / (1024 * 1024):.2f} MB",
            "created_at": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        })
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files


@app.get("/download")
def download(tbmt_id: str, force_refresh: bool = False):
    tid = tbmt_id.strip()
    if not tid:
        raise HTTPException(400, "Missing TBMT code")

    zip_path = os.path.join(CACHE_DIR, f"{tid}.zip")
    if os.path.isfile(zip_path):
        return FileResponse(zip_path, filename=f"{tid}.zip", media_type="application/zip")

    cached = [f for f in os.listdir(CACHE_DIR) if f.startswith(tid) and not f.startswith(".tmp") and not f.startswith(".") and not f.endswith(".zip") and not f.endswith(".json")]
    if cached:
        cached.sort(key=lambda f: os.path.getmtime(os.path.join(CACHE_DIR, f)), reverse=True)
        return FileResponse(os.path.join(CACHE_DIR, cached[0]), filename=cached[0])

    raise HTTPException(404, f"No cached files for {tid}")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse("""<!DOCTYPE html><html lang="vi"><head><meta charset="UTF-8"><title>VNeGP Downloader</title>
<script src="https://cdn.tailwindcss.com"></script></head><body class="bg-slate-50 min-h-screen p-8">
<div class="max-w-3xl mx-auto">
<div class="bg-white p-6 rounded-xl shadow-sm mb-6">
<h1 class="text-2xl font-bold mb-2">VNeGP Downloader</h1>
<p class="text-slate-500 text-sm mb-6">Tu dong tai ho so moi thau tu muasamcong.mpi.gov.vn</p>
<div class="mb-4"><input id="tbmt" placeholder="Nhap ma TBMT (VD: IB2600363942)" class="w-full px-4 py-2 border rounded-lg font-mono text-sm"></div>
<div class="flex">
<button onclick="scan()" class="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 text-sm font-medium">Scan & Tai</button>
</div>
<div id="status" class="mt-4 text-sm hidden"></div>
</div>
<div class="bg-white p-6 rounded-xl shadow-sm">
<div class="flex items-center justify-between mb-4"><h2 class="text-lg font-semibold">Kho cache</h2><button onclick="load();refreshStatus()" class="text-blue-600 text-sm hover:underline">Refresh</button></div>
<div id="agentStatus" class="text-xs text-slate-400 mb-3"></div>
<table class="w-full text-sm"><thead><tr class="text-left text-slate-400"><th>File</th><th>Kich thuoc</th><th>Ngay</th><th></th></tr></thead><tbody id="cb"></tbody></table>
</div></div>
<script>
async function load(){const r=await fetch('/api/cached-files');const d=await r.json();
document.getElementById('cb').innerHTML=d.length===0?'<tr><td colspan="4" class="text-center py-4 text-slate-400">Trong</td></tr>':
d.map(f=>'<tr class="border-t"><td class="py-2 font-mono text-blue-600 max-w-xs truncate">'+f.filename+'</td><td class="py-2 text-slate-500">'+f.size+'</td><td class="py-2 text-slate-400 text-xs">'+f.created_at+'</td><td class="py-2"><a href="/download?tbmt_id='+f.tbmt_id+'" class="text-emerald-600 font-medium">Tai</a></td></tr>').join('');}
async function refreshStatus(){const r=await fetch('/api/status');const d=await r.json();
document.getElementById('agentStatus').textContent='Agent: '+(d.agent.running?'Online':'Offline')+' | Cache: '+d.cache.file_count+' files, '+d.cache.size_mb+' MB | Cooldown: '+d.scanner.cooldown_sec+'s | TBMT: '+(d.cache.tbmt_codes.join(', ')||'(none)');}
async function scan(){const id=document.getElementById('tbmt').value.trim();if(!id)return alert('Nhap ma TBMT');
const b=document.querySelector('button');const s=document.getElementById('status');b.disabled=true;b.textContent='Dang scan...';
s.className='mt-4 text-sm';s.innerHTML='<span class="text-blue-600">Starting...</span>';s.classList.remove('hidden');
const apiUrl='/api/scan/'+encodeURIComponent(id);const opts={method:'POST'};
try{const r=await fetch(apiUrl,opts);if(!r.ok){const e=await r.json();throw new Error(e.detail||e.message);}
const d=await r.json();const pollId=id||d.tbmt_code;if(d.status==='cached'){s.innerHTML='<span class="text-green-600">Already cached! '+d.files.length+' files</span>';b.disabled=false;b.textContent='Scan & Tai';return;}
s.innerHTML='<span class="text-blue-600">Scan started. Polling...</span>';
let p=0;const poll=setInterval(async()=>{p++;
const sr=await fetch('/api/scan/'+encodeURIComponent(pollId)+'/status');const sd=await sr.json();
s.innerHTML='<span class="'+(sd.status==='done'?'text-green-600':sd.status==='error'?'text-red-600':'text-blue-600')+'">'+sd.status.toUpperCase()+': '+sd.progress+' ('+sd.files.length+' files)</span>';
if(sd.status==='done'||sd.status==='error'||p>180){clearInterval(poll);b.disabled=false;b.textContent='Scan & Tai';load();refreshStatus();}},2000);}
catch(e){s.innerHTML='<span class="text-red-600">Error: '+e.message+'</span>';b.disabled=false;b.textContent='Scan & Tai';}}
refreshStatus();load();
</script></body></html>""")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
