# VNeGP Downloader

Công cụ tìm kiếm thông báo mời thầu (TBMT) trên Cổng mua sắm công `muasamcong.mpi.gov.vn` và tải trọn gói hồ sơ mời thầu (HSMT) về máy dưới dạng ZIP.

Backend dùng FastAPI, tự động hóa trình duyệt bằng Playwright, kèm giao diện web để nhập mã TBMT, theo dõi tiến trình và tải file.

## Yêu cầu

- Python 3.10+
- Chromium cho Playwright
- Một **download agent** nội bộ chạy tại `http://localhost:1234`, cung cấp API tải hàng loạt `POST /api/download/files/browser/public`. Địa chỉ này cấu hình qua biến `AGENT_BASE_URL` trong `taihsmt.py`.

## Cài đặt

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install fastapi uvicorn playwright requests urllib3
python -m playwright install chromium
```

## Cấu hình

Tạo file `.env` cùng thư mục với `taihsmt.py`:

| Biến | Mô tả |
| --- | --- |
| `VNEGP_API_KEY` | Khóa bảo vệ endpoint tải file, gửi qua header `X-API-Key`. Để trống nếu không cần xác thực. |

Các giá trị khác (cổng `8000`, thời gian chờ giữa các lần quét, thư mục cache) được khai báo trực tiếp trong mã nguồn.

## Chạy

```bash
python taihsmt.py
```

Mở <http://localhost:8000>, nhập mã TBMT hoặc dán URL thông báo mời thầu rồi bấm **Scan & Tải**. File tải về được lưu trong `vnegp_cache/`.

## API

| Phương thức | Endpoint | Mô tả |
| --- | --- | --- |
| `POST` | `/api/scan` | Quét theo `tbmt_code` hoặc `url` trong body JSON |
| `POST` | `/api/scan/{tbmt_code}` | Quét theo mã TBMT |
| `GET` | `/api/scan/{tbmt_code}/status` | Tiến trình quét |
| `GET` | `/api/files/{tbmt_code}` | Tải ZIP các file đã lưu (yêu cầu `X-API-Key` nếu có cấu hình) |
| `GET` | `/api/status` | Trạng thái agent, cache, dung lượng |
| `GET` | `/api/cached-files` | Danh sách file đã cache |
| `GET` | `/api/daily-scan` | Trạng thái quét tự động trong ngày |
| `GET` | `/api/daily-scan/preview` | Xem trước danh sách TBMT sẽ quét |
| `POST` | `/api/daily-scan/run` | Chạy quét tự động |
| `POST` | `/api/daily-scan/retry` | Quét lại các mã thất bại |
| `GET` | `/api/daily-scan/failed` | Danh sách mã thất bại |
| `GET` | `/download?tbmt_id=...` | Tải file đã cache (dùng cho giao diện) |

Giới hạn tần suất: tối thiểu 30 giây giữa hai lần quét thủ công và chỉ một tiến trình quét tại một thời điểm.

## Quét hàng ngày

Danh sách TBMT trong ngày được lưu tại `vnegp_cache/_daily_scan.json`, chỉ mục file đã tải tại `vnegp_cache/_tbmt_index.json`. Các lần quét trước đó được tái sử dụng nhờ cache.

## Cài đặt như Windows service

`install_service.ps1` cài chương trình thành service tên `VNeGPDownloader` bằng NSSM. Script đang hard-code đường dẫn `nssm.exe`, `python.exe`, thư mục làm việc — chỉnh các biến `$nssm`, `$python`, `$script`, `$workDir` trước khi chạy PowerShell với quyền Administrator:

```powershell
.\install_service.ps1              # cài đặt
.\install_service.ps1 -Uninstall   # gỡ bỏ
```

## Lưu ý

- Chỉ dùng cho mục đích hợp lệ và tuân thủ điều khoản sử dụng của Cổng mua sắm công.
- Playwright cần tải Chromium về máy; agent tải file phải đang chạy thì bước tải HSMT mới thành công.
