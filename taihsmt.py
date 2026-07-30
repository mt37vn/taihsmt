import os, time, re, json, random, zipfile, threading, ssl
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests as req_lib
from requests.adapters import HTTPAdapter
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from playwright.sync_api import sync_playwright
from fastapi.security import APIKeyHeader

# --- Load .env file ---
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

API_KEY = os.environ.get("VNEGP_API_KEY", "")

app = FastAPI(title="VNeGP Downloader")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(api_key: str = Depends(api_key_header)):
    if not API_KEY:
        return
    if not api_key or api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vnegp_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

AGENT_BASE_URL = "http://localhost:1234"
TBMT_CACHE_FILE = os.path.join(CACHE_DIR, "_tbmt_index.json")

_progress_store = {}
_scanner_lock = threading.Lock()
_last_scan_time = 0.0
MIN_SCAN_INTERVAL = 30
_manual_waiting = threading.Event()

_DAILY_SCAN_FILE = os.path.join(CACHE_DIR, "_daily_scan.json")
_daily_scan_lock = threading.Lock()

# --- Playwright lifecycle ---
_pw = None
_pw_browser = None

PW_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

def _new_page():
    global _pw, _pw_browser
    try:
        if _pw_browser and _pw_browser.is_connected():
            return _pw_browser.new_page(user_agent=PW_USER_AGENT)
    except:
        pass
    if _pw:
        try: _pw.stop()
        except: pass
    _pw = sync_playwright().start()
    _pw_browser = _pw.chromium.launch(headless=True)
    return _pw_browser.new_page(user_agent=PW_USER_AGENT)

def _close_browser():
    global _pw, _pw_browser
    if _pw_browser:
        try: _pw_browser.close()
        except: pass
        _pw_browser = None
    if _pw:
        try: _pw.stop()
        except: pass
        _pw = None

@app.on_event("shutdown")
def _shutdown():
    _close_browser()

# --- XHR Interception JS ---
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


# ==========================================
# Section 2: HÀM HỖ TRỢ (from user's bot, verbatim)
# ==========================================

def safe_get_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(map(str, value))
    return str(value)

def format_vnd(x):
    if x is None:
        return ""
    if isinstance(x, (int, float)):
        return f"{x:,.0f} VND"
    try:
        n = float(str(x).replace(",", "").strip())
        return f"{n:,.0f} VND"
    except:
        return str(x)

def normalize_price(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        cleaned = str(x).replace(",", "").replace("VND", "").strip()
        return float(cleaned)
    except:
        return None

def _try_parse_json_maybe(s):
    if not s:
        return None
    if isinstance(s, dict):
        return s
    if isinstance(s, str):
        ss = s.strip()
        try:
            return json.loads(ss)
        except:
            return None
    return None

def parse_date_flexible(date_str):
    if not date_str:
        return None
    formats = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%d/%m/%Y %H:%M", "%d/%m/%Y"]
    for fmt in formats:
        try:
            s = date_str
            if "T" in s and "." not in s:
                s = s[:19]
            return datetime.strptime(s, fmt)
        except:
            continue
    return None


# ==========================================
# Section 3: KHLCNT LOOKUP LOGIC (from user's bot, verbatim)
# ==========================================

def build_khlcnt_detail_url(plan_no: str, plan_id: str) -> str:
    """URL trang chi tiết KHLCNT"""
    # Nếu có ID thì dùng kèm, nếu không thì chỉ dùng planNo (hy vọng web tự redirect)
    base_url = (
        "https://muasamcong.mpi.gov.vn/en/web/guest/contractor-selection?"
        "p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
        "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
        "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=detail-v2"
        "&type=es-plan-project-p&step=tbmt&stepCode=plan-step-1"
        f"&planNo={plan_no}"
    )
    if plan_id:
        base_url += f"&id={plan_id}"
        
    return base_url + "&notifyId=undefined&notifyNo=undefined&bidMode=undefined&bidForm=undefined"

def fetch_khlcnt_detail_json(page, plan_no: str, plan_id: str):
    url = build_khlcnt_detail_url(plan_no, plan_id)
    def is_target_xhr(resp):
        try:
            return (
                resp.status == 200
                and "application/json" in (resp.headers or {}).get("content-type", "")
                and "contractor-selection" in resp.url
            )
        except:
            return False
            
    # Capture Plan Detail Response
    captured_plan_data = []
    def handle_plan_response(response):
        try:
             if is_target_xhr(response):
                # print(f"      📡 Captured Plan API: {response.url}")
                json_data = response.json()
                captured_plan_data.append(json_data)
        except:
            pass

    page.on("response", handle_plan_response)

    try:
        page.goto(url, timeout=20000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"      ⚠️ Fetch Plan Nav Warning: {e}")
        # Don't return None yet, check if we captured data!

    try:
        if captured_plan_data:
            return captured_plan_data[0]
            
        return None
    finally:
        page.remove_listener("response", handle_plan_response)

def fetch_tbmt_detail_json(page, tbmt_id):
    """
    Vào trang chi tiết TBMT để lấy thông tin (đặc biệt là ID của KHLCNT).
    """
    # URL detail TBMT (dùng render=detail-v2)
    # Tuy nhiên để nhẹ, ta có thể gọi thẳng API nếu biết endpoint. 
    # Nhưng để an toàn (bắt chước user), ta navigate vào trang detail.
    url = (
        "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection?"
        "p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
        "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
        "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=detail-v2"
        "&type=es-notify-contractor&step=tbmt"
        f"&id={tbmt_id}&notifyId={tbmt_id}"
    )
    
    # Predicate bắt JSON detail
    # Predicate bắt JSON detail
    # Update: Dùng list để hứng tất cả response JSON
    captured_data = []
    
    def handle_response(response):
        try:
            if (
                response.status == 200 
                and "application/json" in (response.headers or {}).get("content-type", "")
                and "contractor-selection" in response.url
            ):
                # print(f"      📡 Captured: {response.url}")
                json_data = response.json()
                captured_data.append(json_data)
                
                # DEBUG: Nếu là API tiềm năng, in luôn ra màn hình
                if "lcnt_tbmt_ttc_ldt" in response.url:
                    # print("      📡 Captured TBMT Detail API")
                    pass
        except:
            pass

    # Attach listener
    page.on("response", handle_response)

    try:
        # Không dùng expect_response nữa mà dùng wait
        # page.goto có thể timeout nhưng ta vẫn muốn check captured_data
        page.goto(url, timeout=20000)
        page.wait_for_timeout(3000) # Đợi thêm tí cho chắc
    except Exception as e:
        print(f"      ⚠️ Page load warning: {e}")
        # Không return None ngay, mà check captured_data xem vớt vát được gì không
    
    try:
        # Detach listener (optional)
        page.remove_listener("response", handle_response)
        
        if captured_data:
            # Tìm JSON xịn nhất trong đống captured
            # Ưu tiên 1: Có bidpPlanDetail (là cái ta cần)
            for d in captured_data:
                if d.get('bidpPlanDetail') or (d.get('data') and d.get('data').get('bidpPlanDetail')):
                    return d
            
            # Ưu tiên 2: Có planId ở root hoặc data (backup cũ)
            for d in captured_data:
                data_obj = d.get('data') or d
                if data_obj.get('planId') or data_obj.get('idKhlcnt'):
                    return d
        
        # Fallback return anything
        return captured_data[0] if captured_data else None

    except Exception as e:
        print(f"      ⚠️ Processing TBMT Detail failed: {e}")
        return None

def find_plan_id_by_no(page, plan_no):
    """
    Search tìm Plan ID từ Plan No (Fallback logic - Homepage).
    """
    print(f"      🔎 [Fallback] Looking up Plan info for: {plan_no}")
    try:
        # 1. Vào trang chủ (render=index) chứa selector radio-khlcnt như user cung cấp
        search_url = "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection?render=index"
        page.goto(search_url, wait_until="domcontentloaded")
        
        # 2. Click Radio Button KHLCNT
        # Selector user cung cấp: <label for="radio-khlcnt"><input id="radio-khlcnt" ...></label>
        try:
            # Click vào label là chuẩn nhất
            page.locator("label[for='radio-khlcnt']").first.click(timeout=5000)
            
            # Double check: kiểm tra xem input có checked không
            is_checked = page.evaluate("document.querySelector('#radio-khlcnt').checked")
            if not is_checked:
                print("        ⚠️ Radio not checked after label click. Forcing JS click...")
                page.evaluate("document.querySelector('#radio-khlcnt').click()")
                
        except Exception as e:
            print(f"        ⚠️ Click radio KHLCNT failed: {e}")
            # Fallback cuối cùng: Force click input
            try:
                page.locator("#radio-khlcnt").click(timeout=2000, force=True)
            except:
                pass
            
        page.wait_for_timeout(2000) # Chờ UI switch mode
            
        # 3. Nhập từ khóa
        search_input = 'input[name="keyword"]'
        page.wait_for_selector(search_input, timeout=15000)
        page.fill(search_input, plan_no)
        
        # 4. Trigger Search
        with page.expect_response(lambda r: "contractor-selection" in r.url and r.status == 200, timeout=15000) as resp:
            page.keyboard.press("Enter")
            
        data = resp.value.json()
        items = data.get('data') or data.get('page', {}).get('content') or []
        
        # 5. Tìm chính xác planNo
        for item in items:
            p_no = safe_get_text(item.get('planNo') or item.get('notifyNo')).strip()
            if p_no == plan_no:
                return safe_get_text(item.get('id')), item
                
        return None, None
    except Exception as e:
        print(f"      ⚠️ Lookup failed: {e}")
        return None, None

def _extract_price(pkg):
    price = pkg.get("bidPrice") or pkg.get("bidEstimatePrice") or pkg.get("planItemPrice")
    unit = pkg.get("bidPriceUnit") or pkg.get("bidPriceUnitName") or pkg.get("currencyCode")
    return price, unit

def get_khlcnt_price_from_plan(kh_page, plan_cache, plan_no, tbmt_notify_no, tbmt_id):
    """
    Tìm giá dự toán trong Plan.
    Flow mới:
    1. Vào chi tiết TBMT (dùng tbmt_id) -> Lấy planId.
    2. Vào chi tiết Plan (dùng planId hoặc planNo nếu không có ID) -> Lấy giá.
    """
    if not plan_no and not tbmt_id:
        return None, None, None
        
    # Check cache
    guarantee_val = None
    if plan_no and plan_no in plan_cache:
        plan_detail = plan_cache[plan_no]
    else:
        # 1. Tìm ID của Plan từ chi tiết TBMT (Cố gắng lấy ID xịn)
        plan_id = None
        
        # Thử lấy ID từ tbmt detail
        try:
            print(f"      🔗 Fetching TBMT Detail to get Plan ID...")
            tbmt_detail = fetch_tbmt_detail_json(kh_page, tbmt_id)
            if tbmt_detail:
                data_obj = tbmt_detail.get('data') or tbmt_detail
                
                # Extract Guarantee Value (Bid Security)
                # Check bidNotification or bidoNotifyContractorM (older/variant structures)
                bid_notif = (
                    data_obj.get('bidNoContractorResponse', {}).get('bidNotification') 
                    or data_obj.get('bidoNotifyContractorM')
                    or data_obj.get('bidNotification')
                )
                if bid_notif:
                    guarantee_val = bid_notif.get('guaranteeValue')

                plan_id = data_obj.get('planId') or data_obj.get('idKhlcnt')
                
                # Check for nested bidpPlanDetail (common in new API)
                if data_obj.get('bidpPlanDetail'):
                    plan_inner = data_obj['bidpPlanDetail']
                    
                    # Try to extract Price directly!
                    direct_price, direct_unit = _extract_price(plan_inner)
                    direct_price = normalize_price(direct_price)
                    if direct_price is not None:
                        print(f"      💰 Info: Found Price in TBMT Detail. Skipping Plan fetch.")
                        # Simulate plan detail for cache if needed, or just return
                        return direct_price, direct_unit, guarantee_val
                    
                    if not plan_id:
                        plan_id = plan_inner.get('planId') or plan_inner.get('id')
        except Exception as e:
            print(f"      ⚠️ TBMT Detail parse error: {e}")
            
        if not plan_id:
            print(f"      ⚠️ Missing Plan ID. Switching to Search Fallback (Homepage)...")
            # Fallback: Search thủ công
            plan_id, _ = find_plan_id_by_no(kh_page, plan_no)
            
        if not plan_id:
            print("      ❌ Fallback failed. Cannot find Plan ID.")
            return None, None, guarantee_val
        
        print(f"      ✅ Plan ID Found: {plan_id}")
        print(f"      📂 Fetching Plan Detail (No: {plan_no}, ID: {plan_id})...")
        plan_detail = fetch_khlcnt_detail_json(kh_page, plan_no, plan_id)
        if plan_no:
            plan_cache[plan_no] = plan_detail # Save to cache
        
    if not plan_detail:
        return None, None, guarantee_val

    # 3. Tìm gói thầu trong Plan detail khớp notifyNo
    # Flatten list packages
    candidates = []
    for k in ["bidpPlanDetailToProjectList", "bidpPlanDetailList", "bidpPlanDetails"]:
        v = plan_detail.get(k)
        if isinstance(v, list): candidates = v; break
    if not candidates:
        for w in ["detailKhlcntRes", "data", "result"]:
            v = plan_detail.get(w, {})
            if isinstance(v, dict):
                for k in ["bidpPlanDetailToProjectList", "bidpPlanDetailList"]:
                    vv = v.get(k)
                    if isinstance(vv, list): candidates = vv; break
    
    # Matching
    tbmt_notify_no = tbmt_notify_no.strip()
    price, unit = None, None
    for pkg in candidates:
        # Check notifyNo directly
        if safe_get_text(pkg.get('notifyNo')).strip() == tbmt_notify_no:
            price, unit = _extract_price(pkg)
            price = normalize_price(price)
            break
            
        # Check inside linkNotifyInfo
        link_info = _try_parse_json_maybe(pkg.get('linkNotifyInfo'))
        if isinstance(link_info, dict):
            if safe_get_text(link_info.get('notifyNo')).strip() == tbmt_notify_no:
                price, unit = _extract_price(pkg)
                price = normalize_price(price)
                break
                
    return price, unit, guarantee_val


# === ORIGINAL HELPERS (server-only) ===

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

def _is_valid_uuid(val):
    return bool(_UUID_RE.match(str(val))) if val else False

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

def _load_daily_state():
    if os.path.exists(_DAILY_SCAN_FILE):
        try:
            with open(_DAILY_SCAN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"last_run": None, "next_run": None, "status": "idle", "last_total_projects": 0, "last_new_projects": 0, "last_results": {}, "failed_codes": []}

def _save_daily_state(state):
    with open(_DAILY_SCAN_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

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

def _extract_hsmt_params(page):
    from urllib.parse import urlparse, parse_qs
    current_url = page.url
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query)
    notify_id = (qs.get("notifyId", [None]) or [None])[0]
    process_apply = (qs.get("processApply", ["LDT"]) or ["LDT"])[0]
    if not notify_id:
        raise Exception("Cannot extract notifyId from page URL")
    auth_token = page.evaluate("Liferay.authToken")
    if not auth_token:
        raise Exception("Cannot extract Liferay.authToken from page")
    pw_cookies = page.context.cookies()
    cookie_dict = {c["name"]: c["value"] for c in pw_cookies if c.get("name") and c.get("value")}
    return notify_id, process_apply, auth_token, cookie_dict

def find_bid_detail_url(page, tbmt_code):
    page.goto("https://muasamcong.mpi.gov.vn/web/guest/contractor-selection?render=index")
    try:
        page.wait_for_selector("input[name='keyword'], input.form-control, input[type='text']", timeout=8000)
    except:
        time.sleep(5)

    search_input = None
    for sel in ["input[name='keyword']", "input.form-control", "input[type='text']"]:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                search_input = el
                break
        except:
            continue
    if not search_input:
        try:
            el = page.locator("input").first
            if el.is_visible():
                search_input = el
        except:
            pass
    if not search_input:
        raise Exception("Cannot find search input")

    search_input.click()
    time.sleep(0.3)
    search_input.fill("")
    for ch in tbmt_code:
        page.keyboard.type(ch, delay=random.randint(80, 200))
    time.sleep(1)

    with page.expect_response(
        lambda r: "contractor-selection" in r.url and r.status == 200,
        timeout=15000,
    ) as resp_info:
        page.keyboard.press("Enter")
    try:
        resp_info.value
    except:
        pass
    time.sleep(4)

    result_link = None
    for sel in [
        f"a[href*='notifyNo={tbmt_code}']",
        f"a[href*='{tbmt_code}']",
        "a[href*='detail-v2']",
        "a.tbmt-title",
    ]:
        try:
            links = page.locator(sel)
            count = links.count()
            for i in range(count):
                link = links.nth(i)
                href = link.get_attribute("href") or ""
                text = link.text_content() or ""
                if tbmt_code in href or tbmt_code in text:
                    result_link = link
                    break
            if result_link:
                break
        except:
            continue
    if not result_link:
        try:
            links = page.locator("a[href*='contractor-selection']")
            count = links.count()
            for i in range(count):
                link = links.nth(i)
                href = link.get_attribute("href") or ""
                text = link.text_content() or ""
                if tbmt_code in href or tbmt_code in text:
                    result_link = link
                    break
        except:
            pass
    if not result_link:
        raise Exception(f"No result found for {tbmt_code}")

    result_link.scroll_into_view_if_needed()
    time.sleep(1)
    result_link.click()
    time.sleep(6)
    return page.url


def scan_and_download_tbmt(tbmt_code: str, detail_url: str = None):
    import shutil

    tbmt_dir = os.path.join(CACHE_DIR, f".tmp_{tbmt_code}")
    os.makedirs(tbmt_dir, exist_ok=True)

    page = _new_page()
    try:
        def _prog(msg):
            _progress_store[tbmt_code] = _progress_store.get(tbmt_code, {"status": "scanning", "progress": "", "files": []})
            _progress_store[tbmt_code]["progress"] = msg

        if detail_url:
            _prog("Navigating to detail page...")
            _sprint(f"1. Navigating directly to detail page for {tbmt_code}...")
            page.goto(detail_url, timeout=30000)
            try:
                page.wait_for_selector(".nav-tabs, [class*='tab'], .portlet-body", timeout=8000)
            except:
                time.sleep(8)
        else:
            _prog("Searching for TBMT posting...")
            _sprint(f"1. Searching and navigating to detail page for {tbmt_code}...")
            url = find_bid_detail_url(page, tbmt_code)
            _prog("Loading detail page...")
            _sprint(f"   URL: {url}")

        _prog("Extracting API params...")
        _sprint("2. Extracting HSMT API params and calling API directly...")
        file_items = []
        direct_ok = False
        try:
            notify_id, process_apply, auth_token, cookies = _extract_hsmt_params(page)
            hsmt_response = _call_hsmt_api_direct(notify_id, process_apply, auth_token, cookies)
            file_items = _parse_hsmt_formvalue(hsmt_response)
            direct_ok = True
            _sprint(f"   Direct API: {len(file_items)} file items extracted")
        except Exception as e:
            _sprint(f"   Direct API attempt failed: {e}")
            _sprint("   Falling back to XHR interception path...")

        if not direct_ok or not file_items:
            if direct_ok and not file_items:
                _sprint("   Direct API returned 0 files, falling back to XHR interception...")
            _prog("Injecting XHR interceptor...")
            _sprint("3. Injecting XHR interceptor...")
            page.evaluate(XHR_INTERCEPT_JS)

            _prog("Waiting for page load...")
            _sprint("4. Waiting for initial page load...")
            time.sleep(8)

            _prog("Looking for HSMT tab...")
            _sprint("5. Finding 'Hồ sơ mời thầu' tab...")
            hsmt_tab_clicked = False
            try:
                tabs = page.locator(".nav-tabs a, ul.nav a, [class*='tab'] a")
                count = tabs.count()
                for i in range(count):
                    el = tabs.nth(i)
                    text = (el.text_content() or "").strip()
                    href = (el.get_attribute("href") or "").lower()
                    if "hồ sơ mời thầu" in text.lower() or "hs-mt" in href or "hsmot" in href:
                        el.click()
                        _sprint(f"   Clicked tab: {text}")
                        hsmt_tab_clicked = True
                        time.sleep(4)
                        break
            except:
                pass
            if not hsmt_tab_clicked:
                for tab_sel in ["a[href*='tender-invitation']", "a[href*='notify-contractor-step-1-tbmt']", ".nav-tabs a:nth-child(2)"]:
                    try:
                        el = page.locator(tab_sel).first
                        if el.is_visible():
                            el.click()
                            _sprint(f"   Fallback tab: {tab_sel}")
                            time.sleep(4)
                            break
                    except:
                        continue

            _prog("Waiting for XHR data...")
            _sprint("6. Waiting for XHR data to arrive...")
            time.sleep(8)

            _prog("Capturing XHR responses...")
            _sprint("7. Capturing XHR responses and extracting file IDs from formValue...")
            xhr_raw = page.evaluate("JSON.stringify(window.__xhrResponses || [])")
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
            _prog(f"No files found for {tbmt_code}")
            raise Exception(f"No file IDs found in HSMT response for {tbmt_code}")

        _prog("Downloading files from agent...")
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

        _prog("Saving ZIP file...")
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
            if info.is_dir():
                continue
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
            page.close()
        except:
            pass
        try:
            shutil.rmtree(tbmt_dir, ignore_errors=True)
        except:
            pass


# === Daily Auto-Scan from haproinfo ===

HAPROINFO_URL = "https://tinhgiathau.haproinfo.vn/api/project-ids"

def _run_daily_scan():
    if _daily_scan_lock.locked():
        _sprint("[daily-scan] Already running, skipped")
        return {"error": "Already running"}
    with _daily_scan_lock:
        _sprint("[daily-scan] Starting...")
        state = _load_daily_state()
        state["status"] = "running"
        state["last_results"] = {}
        _save_daily_state(state)
        try:
            resp = req_lib.get(HAPROINFO_URL, timeout=15, verify=False)
            data = resp.json()
            projects = data.get("projects", [])
            all_codes = [p["maDauThau"] for p in projects]
            _sprint(f"[daily-scan] haproinfo returned {len(all_codes)} projects")
        except Exception as e:
            _sprint(f"[daily-scan] Failed to fetch haproinfo API: {e}")
            state["status"] = "failed"
            state["last_run"] = datetime.now().isoformat()
            state["next_run"] = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            _save_daily_state(state)
            return {"error": str(e)}
        idx = _load_tbmt_index()
        failed_map = state.get("failed_codes", {})
        new_codes = [c for c in all_codes if c not in idx and failed_map.get(c, 0) < 3]
        retry_codes = [c for c in all_codes if c in failed_map and failed_map.get(c, 0) < 3 and c not in idx]
        _sprint(f"[daily-scan] {len(new_codes)} new/retry projects (of {len(all_codes)} total, {len(retry_codes)} retries)")
        state["last_total_projects"] = len(all_codes)
        state["last_new_projects"] = len(new_codes)
        state["last_run"] = datetime.now().isoformat()
        if not new_codes:
            _sprint("[daily-scan] No new projects to scan")
            state["status"] = "completed"
            state["next_run"] = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            _save_daily_state(state)
            return {"total": len(all_codes), "new": 0, "results": {}}
        for i, code in enumerate(new_codes):
            _sprint(f"[daily-scan] [{i+1}/{len(new_codes)}] Scanning {code}...")
            state["last_results"][code] = "scanning"
            _save_daily_state(state)
            while _manual_waiting.is_set():
                _sprint(f"[daily-scan] Manual scan in progress, waiting 10s...")
                state["status"] = "paused"
                state["last_results"][code] = "cho manual"
                _save_daily_state(state)
                time.sleep(10)
            state["status"] = "running"
            state["last_results"][code] = "scanning"
            _save_daily_state(state)
            try:
                result = scan_and_download_tbmt(code)
                files = [r["filename"] for r in result]
                state["last_results"][code] = "done"
                if code in state.get("failed_codes", {}):
                    del state["failed_codes"][code]
                    _sprint(f"[daily-scan] {code}: removed from failed_codes")
                _sprint(f"[daily-scan] {code}: {len(files)} files downloaded")
            except Exception as e:
                state["last_results"][code] = f"error: {e}"
                if "failed_codes" not in state: state["failed_codes"] = {}
                state["failed_codes"][code] = state["failed_codes"].get(code, 0) + 1
                attempt = state["failed_codes"][code]
                if attempt >= 3:
                    _sprint(f"[daily-scan] {code}: FAILED x{attempt} - stopped retrying")
                else:
                    _sprint(f"[daily-scan] {code}: FAILED x{attempt} - will retry ({3 - attempt} left)")
                _sprint(f"[daily-scan] {code}: FAILED - {e}")
            _save_daily_state(state)
            if i < len(new_codes) - 1:
                _sprint(f"[daily-scan] Cooldown 30s before next scan...")
                time.sleep(30)
        _sprint(f"[daily-scan] Complete - {len(new_codes)} projects processed")
        state["status"] = "completed"
        state["next_run"] = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        _save_daily_state(state)
        return {"total": len(all_codes), "new": len(new_codes), "results": state["last_results"]}

def _daily_scan_loop():
    now = datetime.now()
    next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    delay = (next_midnight - now).total_seconds()
    _sprint(f"[daily-scan] First run in {int(delay)}s (at 00:00 midnight)")
    time.sleep(delay)
    while True:
        _run_daily_scan()
        state = _load_daily_state()
        next_ts = state.get("next_run") or time.time() + 86400
        delay = max(60, next_ts - time.time())
        _sprint(f"[daily-scan] Next run in {int(delay)}s")
        time.sleep(delay)

@app.on_event("startup")
def _start_daily_scheduler():
    state = _load_daily_state()
    if state.get("status") in ("running", "completed"):
        _sprint("[daily-scan] Clearing stale state on startup")
    state["status"] = "idle"
    state["last_results"] = {}
    state["next_run"] = None
    if "failed_codes" not in state:
        state["failed_codes"] = {}
    elif isinstance(state["failed_codes"], list):
        state["failed_codes"] = {c: 1 for c in state["failed_codes"]}
    _save_daily_state(state)
    t = threading.Thread(target=_daily_scan_loop, daemon=True)
    t.start()
    _sprint("[daily-scan] Scheduler started (first run at midnight 00:00)")

# === FastAPI Endpoints ===

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
        _manual_waiting.set()
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
        finally:
            _manual_waiting.clear()

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


@app.get("/api/files/{tbmt_code}", dependencies=[Depends(verify_api_key)])
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
    zip_files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".zip")]
    total_size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in zip_files if os.path.isfile(os.path.join(CACHE_DIR, f)))

    return {
        "agent": {"running": agent_ok, "url": AGENT_BASE_URL},
        "cache": {
            "directory": CACHE_DIR,
            "file_count": len(zip_files),
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
        if not os.path.isfile(fp) or fn.startswith(".") or fn.startswith(".tmp") or fn.endswith(".json") or fn.endswith(".log"):
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


@app.get("/api/daily-scan")
def api_daily_scan_status():
    state = _load_daily_state()
    if _daily_scan_lock.locked() and state.get("status") != "running":
        state["status"] = "running"
    return state


@app.get("/api/daily-scan/preview")
def api_daily_scan_preview():
    try:
        resp = req_lib.get(HAPROINFO_URL, timeout=15, verify=False)
        data = resp.json()
        projects = data.get("projects", [])
        all_codes = [p["maDauThau"] for p in projects]
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch haproinfo: {e}")
    idx = _load_tbmt_index()
    state = _load_daily_state()
    failed_map = state.get("failed_codes", {})
    new_codes = [c for c in all_codes if c not in idx and failed_map.get(c, 0) < 3]
    return {"total": len(all_codes), "new": new_codes, "new_count": len(new_codes)}


@app.post("/api/daily-scan/run")
def api_daily_scan_run(background_tasks: BackgroundTasks):
    if _daily_scan_lock.locked():
        state = _load_daily_state()
        return {"status": state.get("status", "running"), "message": "Already running"}
    def _bg():
        _run_daily_scan()
    background_tasks.add_task(_bg)
    return {"status": "started", "message": "Daily scan started in background"}


@app.post("/api/daily-scan/retry")
def api_daily_scan_retry(background_tasks: BackgroundTasks, body: dict = {}):
    code = (body.get("code") or "").strip() if body else ""
    state = _load_daily_state()
    failed = state.get("failed_codes", {})
    if code:
        if code not in failed:
            raise HTTPException(404, f"{code} not in failed list")
        targets = [code]
    else:
        targets = list(failed.keys())
    if not targets:
        raise HTTPException(400, "No failed codes to retry")
    state["status"] = "running"
    _save_daily_state(state)
    def _bg():
        try:
            for c in targets:
                _sprint(f"[retry] Retrying {c}...")
                st = _load_daily_state()
                st["last_results"][c] = "scanning"
                _save_daily_state(st)
                try:
                    with _scanner_lock:
                        result = scan_and_download_tbmt(c)
                    st = _load_daily_state()
                    if c in st.get("failed_codes", {}):
                        del st["failed_codes"][c]
                    st["last_results"][c] = "done"
                    _save_daily_state(st)
                    _sprint(f"[retry] {c}: OK ({len(result)} files)")
                except Exception as e:
                    st = _load_daily_state()
                    st["last_results"][c] = f"error: {e}"
                    _save_daily_state(st)
                    _sprint(f"[retry] {c}: FAILED - {e}")
        finally:
            st = _load_daily_state()
            st["status"] = "idle"
            _save_daily_state(st)
    background_tasks.add_task(_bg)
    return {"status": "started", "targets": targets}


@app.get("/api/daily-scan/failed")
def api_daily_scan_failed():
    state = _load_daily_state()
    return {"failed_codes": state.get("failed_codes", {})}


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
    return HTMLResponse("""<!DOCTYPE html><html lang="vi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>VNeGP Downloader</title>
<script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-50 min-h-screen p-3 sm:p-8">
<div id="loginOverlay" class="fixed inset-0 z-50 flex items-center justify-center bg-slate-800/80">
<div class="bg-white p-6 rounded-xl shadow-xl w-80 max-w-full">
<h2 class="text-lg font-bold mb-4">VNeGP Downloader</h2>
<input id="pwInput" type="password" placeholder="Mat khau" autocomplete="off" class="w-full px-4 py-2.5 border rounded-lg text-sm mb-3 min-h-[44px]">
<button onclick="checkPw()" class="w-full bg-blue-600 text-white py-2.5 rounded-lg hover:bg-blue-700 text-sm font-medium min-h-[44px]">Vao trang</button>
<div id="pwError" class="text-red-600 text-xs mt-2 hidden">Sai mat khau</div>
</div></div>
<div id="app" class="hidden max-w-full sm:max-w-3xl mx-auto">

<div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm mb-4 sm:mb-6">
<h1 class="text-xl sm:text-2xl font-bold mb-1">VNeGP Downloader</h1>
<p class="text-slate-500 text-xs sm:text-sm mb-4 sm:mb-6">Tu dong tai ho so moi thau tu muasamcong.mpi.gov.vn</p>
<div class="relative mb-3">
<input id="tbmt" placeholder="Nhap ma TBMT (VD: IB2600363942)" autocomplete="off" class="w-full px-4 py-2.5 sm:py-2 border rounded-lg font-mono text-sm min-h-[44px]">
<div id="suggestions" class="absolute z-10 w-full bg-white border rounded-lg mt-1 shadow-lg max-h-48 overflow-y-auto hidden"></div>
</div>
<div id="result" class="hidden mb-3 p-3 bg-emerald-50 border border-emerald-200 rounded-lg">
<div class="text-emerald-700 font-medium text-sm mb-1">Da co trong cache &#10003;</div>
<div id="resultInfo" class="text-xs text-slate-500 mb-2"></div>
<a id="resultLink" href="#" class="inline-block bg-emerald-600 text-white px-5 py-2.5 sm:py-2 rounded-lg hover:bg-emerald-700 text-sm font-medium min-h-[44px]">Tai xuong</a>
</div>
<button id="scanBtn" onclick="scan()" class="hidden bg-blue-600 text-white px-6 py-2.5 sm:py-2 rounded-lg hover:bg-blue-700 text-sm font-medium min-h-[44px]">Scan & Tai</button>
<div id="status" class="mt-3 text-sm hidden"></div>
</div>

<div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm">
<div onclick="toggleAdmin()" class="flex items-center justify-between cursor-pointer select-none min-h-[44px]">
<h2 class="text-base sm:text-lg font-semibold">Quan ly</h2>
<span id="adminChevron" class="text-slate-400 text-xl transition-transform duration-200">&#9654;</span>
</div>
<div id="adminContent" class="hidden mt-3">
<div class="flex items-center justify-between mb-3">
<button onclick="load();" class="text-blue-600 text-sm hover:underline min-h-[44px]">Refresh cache</button>
<button onclick="runDaily()" class="bg-emerald-600 text-white px-4 py-2.5 sm:py-1.5 rounded-lg hover:bg-emerald-700 text-xs font-medium min-h-[44px]">Chay tu dong</button></div>
<div id="dailyStatus" class="text-xs text-slate-500 mb-2"></div>
<div class="flex gap-1 mb-2 border-b">
<button id="tabActive" onclick="switchTab('active')" class="px-3 py-1.5 text-xs font-medium border-b-2 border-blue-600 text-blue-600">Dang xu ly</button>
<button id="tabFailed" onclick="switchTab('failed')" class="px-3 py-1.5 text-xs font-medium text-slate-400">Loi <span id="failedCount" class="ml-0.5"></span></button>
</div>
<div id="activeTable" class="overflow-x-auto">
<table class="w-full text-sm"><thead><tr class="text-left text-slate-400"><th>File</th><th>Kich thuoc</th><th>Ngay</th><th></th></tr></thead><tbody id="cb"></tbody></table>
</div>
<div id="failedTable" class="overflow-x-auto hidden">
<div class="flex justify-end mb-1"><button onclick="retryFailed()" class="text-blue-600 text-xs hover:underline min-h-[44px]">Retry tat ca</button></div>
<table class="w-full text-sm"><thead><tr class="text-left text-slate-400"><th>Ma TBMT</th><th>Loi</th><th></th></tr></thead><tbody id="cbFailed"></tbody></table>
</div></div></div>

</div>
<script>
function checkPw(){
  if(document.getElementById('pwInput').value==='inf0@hapro'){
    sessionStorage.setItem('vnegp_auth','1');
    document.getElementById('loginOverlay').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    loadDaily();load();
  }else{
    const e=document.getElementById('pwError');
    e.classList.remove('hidden');
    setTimeout(()=>e.classList.add('hidden'),2000);
  }
}
// Auto-attach API key to all /api/* requests
const origFetch = window.fetch;
window.fetch = function(url, opts){
  if(typeof url==='string'&&url.startsWith('/api/')){
    opts=opts||{};
    opts.headers=opts.headers||{};
    opts.headers['X-API-Key']=sessionStorage.getItem('vnegp_api_key')||'';
  }
  return origFetch.call(this,url,opts);
};
if(sessionStorage.getItem('vnegp_auth')==='1'){
  document.getElementById('loginOverlay').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
}
document.getElementById('pwInput').addEventListener('keydown',function(e){if(e.key==='Enter')checkPw();});
function toggleAdmin(){
  const c=document.getElementById('adminContent');
  const ch=document.getElementById('adminChevron');
  const open=!c.classList.toggle('hidden');
  ch.innerHTML=open?'&#9660;':'&#9654;';
  if(open){load();loadDaily();}
}
window.tbmtCacheMap={};
let debounceTimer;
function doSearch(val){
  const suggestEl=document.getElementById('suggestions');
  const resultEl=document.getElementById('result');
  const scanBtn=document.getElementById('scanBtn');
  const statusEl=document.getElementById('status');
  if(!val){suggestEl.classList.add('hidden');resultEl.classList.add('hidden');scanBtn.classList.add('hidden');statusEl.classList.add('hidden');return;}
  const exact=window.tbmtCacheMap[val];
  if(exact){
    suggestEl.classList.add('hidden');
    scanBtn.classList.add('hidden');
    document.getElementById('resultInfo').textContent='File: '+exact.filename+' ('+exact.size+') | '+exact.created_at;
    document.getElementById('resultLink').href='/download?tbmt_id='+encodeURIComponent(val);
    resultEl.classList.remove('hidden');
    statusEl.classList.add('hidden');
  }else{
    resultEl.classList.add('hidden');
    const matches=Object.keys(window.tbmtCacheMap).filter(k=>k.startsWith(val)).slice(0,20);
    if(matches.length>0){
      suggestEl.innerHTML=matches.map(k=>{
        const f=window.tbmtCacheMap[k];
        return '<div onclick="selectSuggestion(\\''+k+'\\')" class="px-3 py-2.5 sm:py-2 hover:bg-blue-50 cursor-pointer border-b last:border-0 text-sm">'+
          '<span class="font-mono text-blue-600">'+k+'</span> '+
          '<span class="text-slate-400 text-xs">'+f.filename+' ('+f.size+')</span></div>';
      }).join('');
      suggestEl.classList.remove('hidden');
    }else{
      suggestEl.classList.add('hidden');
    }
    scanBtn.classList.remove('hidden');
  }
}
function selectSuggestion(id){
  document.getElementById('tbmt').value=id;
  document.getElementById('suggestions').classList.add('hidden');
  doSearch(id);
}
let activeTab='active';
function switchTab(tab){
  activeTab=tab;
  document.getElementById('activeTable').classList.toggle('hidden',tab!=='active');
  document.getElementById('failedTable').classList.toggle('hidden',tab!=='failed');
  document.getElementById('tabActive').className='px-3 py-1.5 text-xs font-medium border-b-2'+(tab==='active'?' border-blue-600 text-blue-600':' border-transparent text-slate-400');
  document.getElementById('tabFailed').className='px-3 py-1.5 text-xs font-medium border-b-2'+(tab==='failed'?' border-blue-600 text-blue-600':' border-transparent text-slate-400');
}
async function retryFailed(){
  const b=event.target;b.disabled=true;b.textContent='Dang retry...';
  try{const r=await fetch('/api/daily-scan/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Failed');
  setTimeout(()=>{load();loadDaily();},2000);}catch(e){alert(e.message);}
  setTimeout(()=>{b.disabled=false;b.textContent='Retry tat ca';},5000);}
document.getElementById('tbmt').addEventListener('input',function(){
  clearTimeout(debounceTimer);
  debounceTimer=setTimeout(()=>{doSearch(this.value.trim().toUpperCase());},300);
});
document.getElementById('tbmt').addEventListener('blur',function(){
  setTimeout(()=>document.getElementById('suggestions').classList.add('hidden'),200);
});
document.getElementById('tbmt').addEventListener('focus',function(){
  const v=this.value.trim().toUpperCase();
  if(v&&!window.tbmtCacheMap[v])doSearch(v);
});
async function loadDaily(){try{const r=await fetch('/api/daily-scan');const d=await r.json();
const s=d.status||'idle';const c={idle:'text-slate-400',running:'text-blue-600',paused:'text-amber-600',completed:'text-green-600',failed:'text-red-600'};
let txt='Trang thai: <span class="'+(c[s]||'')+' font-medium">'+s+'</span> | Tong: '+d.last_total_projects+' | Moi: '+d.last_new_projects+(d.last_run?' | Luc: '+d.last_run:'');
const sr=await fetch('/api/status');const sd=await sr.json();
txt+=' | Cache: '+sd.cache.file_count+' files, '+sd.cache.size_mb+' MB';
document.getElementById('dailyStatus').innerHTML=txt;}catch(e){document.getElementById('dailyStatus').textContent='Loi: '+e.message;}}
async function runDaily(){const b=event.target;b.disabled=true;b.textContent='Dang kiem tra...';
try{const p=await fetch('/api/daily-scan/preview');const pj=await p.json();
if(!pj.new_count){alert('Khong co du an moi can cap nhat');b.disabled=false;b.textContent='Chay tu dong';return;}
await load();b.textContent='Dang chay...';
const r=await fetch('/api/daily-scan/run',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Failed');}
catch(e){alert(e.message);}b.disabled=false;b.textContent='Chay tu dong';
setTimeout(loadDaily,2000);}
async function load(){const r=await fetch('/api/cached-files');const d=await r.json();
window.tbmtCacheMap={};d.forEach(f=>{window.tbmtCacheMap[f.tbmt_id]=f;});
const dr=await fetch('/api/daily-scan');const ds=await dr.json();
const rs=ds.last_results||{};const seen=new Set(d.map(f=>f.tbmt_id));
const d_rows=Object.entries(rs).filter(([k,v])=>!seen.has(k)&&!v.startsWith('error')).map(([k,v])=>{
  const sc=v==='done'?'text-green-600':v.startsWith('error')||v==='cho manual'?'text-amber-600':'text-blue-600';
  const lb=v==='done'?'xong':v.startsWith('error')?'loi':v==='cho manual'?'cho manual':'dang scan';
  return '<tr class="border-t"><td class="py-2 font-mono max-w-xs truncate '+sc+'">'+k+'</td><td class="py-2 text-slate-400">-</td><td class="py-2 text-slate-400 text-xs">-</td><td class="py-2"><span class="text-xs '+sc+'">'+lb+'</span></td></tr>';
}).join('');
document.getElementById('cb').innerHTML=d.length===0&&!d_rows?'<tr><td colspan="4" class="text-center py-4 text-slate-400">Trong</td></tr>':
d_rows+d.map(f=>'<tr class="border-t"><td class="py-2 font-mono text-blue-600 max-w-xs truncate">'+f.filename+'</td><td class="py-2 text-slate-500">'+f.size+'</td><td class="py-2 text-slate-400 text-xs">'+f.created_at+'</td><td class="py-2"><a href="/download?tbmt_id='+f.tbmt_id+'" class="text-emerald-600 font-medium">Tai</a></td></tr>').join('');
const fr=await fetch('/api/daily-scan/failed');const fj=await fr.json();
const fc=fj.failed_codes||[];const fseen=new Set(d.map(f=>f.tbmt_id));
const fcKeys=Array.isArray(fc)?fc:Object.keys(fc);
const f_rows=fcKeys.filter(k=>!fseen.has(k)).map(k=>{
  const retries=fc[k]||1;
  const err=((rs[k]||'').replace('error: ','')||(retries>1?'Loi lan '+retries:'Chua retry'))+' | Con '+(3-retries)+' lan';
  return '<tr class="border-t"><td class="py-2 font-mono text-red-600">'+k+'</td><td class="py-2 text-xs text-slate-500 max-w-xs truncate">'+err+'</td><td class="py-2"><button onclick="retryOne(\"'+k+'\")" class="text-blue-600 text-xs hover:underline">Retry</button></td></tr>';
}).join('');
document.getElementById('cbFailed').innerHTML=fcKeys.length===0?'<tr><td colspan="3" class="text-center py-4 text-slate-400">Khong co loi</td></tr>':f_rows;
document.getElementById('failedCount').textContent=fcKeys.length?'('+fcKeys.length+')':'';}
async function retryOne(c){try{const r=await fetch('/api/daily-scan/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:c})});if(!r.ok)return;}catch(e){}setTimeout(()=>{load();loadDaily();},2000);}
async function scan(){const id=document.getElementById('tbmt').value.trim().toUpperCase();if(!id)return alert('Nhap ma TBMT');
const b=document.getElementById('scanBtn');const s=document.getElementById('status');
b.disabled=true;b.textContent='Dang scan...';
s.className='mt-3 text-sm';s.innerHTML='<span class="text-blue-600">Starting...</span>';s.classList.remove('hidden');
const apiUrl='/api/scan/'+encodeURIComponent(id);const opts={method:'POST'};
try{const r=await fetch(apiUrl,opts);if(!r.ok){const e=await r.json();throw new Error(e.detail||e.message);}
const d=await r.json();const pollId=id||d.tbmt_code;
if(d.status==='cached'){s.innerHTML='<span class="text-green-600">Already cached! '+d.files.length+' files</span>';b.disabled=false;b.textContent='Scan & Tai';return;}
s.innerHTML='<span class="text-blue-600">Scan started. Polling...</span>';
let p=0;const poll=setInterval(async()=>{p++;
const sr=await fetch('/api/scan/'+encodeURIComponent(pollId)+'/status');const sd=await sr.json();
s.innerHTML='<span class="'+(sd.status==='done'?'text-green-600':sd.status==='error'?'text-red-600':'text-blue-600')+'">'+sd.status.toUpperCase()+': '+sd.progress+' ('+sd.files.length+' files)</span>';
if(sd.status==='done'||sd.status==='error'||p>180){clearInterval(poll);
b.disabled=false;b.textContent='Scan & Tai';b.classList.add('hidden');
if(sd.status==='done'){await load();loadDaily();const exact=window.tbmtCacheMap[id];if(exact){document.getElementById('resultInfo').textContent='File: '+exact.filename+' ('+exact.size+') | '+exact.created_at;document.getElementById('resultLink').href='/download?tbmt_id='+encodeURIComponent(id);document.getElementById('result').classList.remove('hidden');s.classList.add('hidden');}}}},2000);}
catch(e){s.innerHTML='<span class="text-red-600">Error: '+e.message+'</span>';b.disabled=false;b.textContent='Scan & Tai';}}
if(sessionStorage.getItem('vnegp_auth')==='1'){loadDaily();load();}
</script></body></html>""")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)










