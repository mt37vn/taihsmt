# VNeGP EGP Agent - Discovery Notes
## 2026-07-29

### Architecture
```
[muasamcong.mpi.gov.vn] <--HTTPS--> [EGP Browser Extension] <--localhost:1234--> [EGP Agent (Spring Boot)]
                                                                                      |
                                                                                      v
                                                                       C:\MPI\EGP-AGENT\DOWNLOAD\
```

### EGP Agent (localhost:1234)
- Spring Boot (com.fis.egp.ServiceApp), Java 17, port 1234
- **Bulk download:** `GET /api/download/files/browser/public?mapFiles={json}&fileName=out.zip`
- **Single download:** `GET /download/file/browser/public?fileId={uuid}` (returns 403 with wrong origin)
- Auth: `Origin: https://muasamcong.mpi.gov.vn` header only (no cookies, no auth token)
- Backend proxy: `https://filemsc.muasamcong.gov.vn/edoc-proxy-service/api/un-security/start/download/file`
- Cleans up `C:\MPI\EGP-AGENT\DOWNLOAD\` after serving files

### Portal Page Architecture
- **Liferay Portal with YUI3** (not Vue.js)
- File data via XHR to `/o/egp-portal-contractor-selection-v2/services/`
- HSMT API (`lcnt_tbmt_hsmt`) returns `bidoInvBiddingDTO[]` with `formValue` containing `fileId`+`fileName` pairs
- `.tags-fileAttach` elements have no onclick/href/data-file-id — the Chrome extension handles clicks

### How it Works (Discovered via Loopback Capture)
1. Page loads HSMT data from `lcnt_tbmt_hsmt` API → `formValue` JSON contains `fileId`+`fileName` pairs
2. Chrome extension intercepts `.tags-fileAttach` clicks via content script
3. Extension extracts the file UUIDs from the HSMT `formValue` data
4. Extension calls `GET /api/download/files/browser/public` with `mapFiles` JSON and `Origin` header
5. Agent downloads files from the proxy backend, packages them as ZIP, returns `application/octet-stream`

### Key Findings
- **Endpoint:** `/api/download/files/browser/public` (NOT `/download/file/browser/public`)
- **Parameter:** `mapFiles={"uuid":"filename",...}` + `fileName=out.zip`
- **HSMT UUIDs ARE the correct file IDs** — no translation layer
- **Auth:** `Origin: https://muasamcong.mpi.gov.vn` only — no cookies, no auth token
- **Transport:** Standard HTTP `fetch()` — no WebSocket, no Native Messaging, no custom protocol
- **IsTrusted:** Not relevant — the endpoint works from Python directly

### Direct HSMT API Call (July 2026 Optimization)
Instead of clicking the HSMT tab and intercepting XHR via monkey-patching, the Python `taihsmt.py` now calls the HSMT API directly:

```
POST https://muasamcong.mpi.gov.vn/o/egp-portal-contractor-selection-v2/services/lcnt_tbmt_hsmt?token={Liferay.authToken}
Content-Type: application/json
Body: {"id": "<notifyId>", "processApply": "<processApply>"}
```

**Discovery details:**
- The `token` query param accepts `Liferay.authToken` (short, standard Liferay CSRF token) — NOT the long opaque token captured in earlier XHR dumps
- `notifyId` and `processApply` are extracted from the detail page URL query string (e.g. `?notifyId=c47f1699-...&processApply=LDT`)
- The response `bidoInvBiddingDTO[]` array contains `formValue` JSON with nested `{fileId, fileName}` pairs
- Browser cookies must be forwarded (the portal validates session cookies)
- SSL: portal uses weak DH keys rejected by Python 3.14+ defaults; requires `ctx.set_ciphers("DEFAULT:@SECLEVEL=0")` + `CERT_NONE`

**Performance impact:**
- Old path (XHR interception): ~30 seconds per scan (12s page load + 8s XHR inject wait + 4s tab click + 8s XHR wait)
- New path (direct API): ~12 seconds per scan (10s page load + 2s API call)
- Fallback to old path preserved in case direct API fails

### Node.js Runtime Workaround
The EGP Agent runs on Java 17 (Spring Boot). The portal's Liferay portlet uses YUI3 (not Vue.js/React). API calls from the portal to the agent require only the `Origin` header — no cookies or tokens.

### Remaining Issues
- Some attachments may be in tabs/sections not loaded by the HSMT tab
- Different TBMT types may use different endpoints
- `find_bid_detail_url()` still relies on portal search (slow, ~30s). Could potentially be replaced with direct search API call
- Full API surface of the agent still undocumented
