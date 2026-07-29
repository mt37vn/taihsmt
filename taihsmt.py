import os, time, re, json, random, zipfile, threading, ssl
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests as req_lib
from requests.adapters import HTTPAdapter
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from playwright.sync_api import sync_playwright

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
