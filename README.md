# Seismic MCP Server

Python MCP Server สำหรับค้นหาเหตุแผ่นดินไหวจาก USGS และ TMD จับคู่เหตุการณ์กับทรัพย์สิน และคำนวณเงินจ่ายตามเงื่อนไข parametric

## รันด้วย Docker Compose

จากโฟลเดอร์หลักของ repo

```powershell
docker compose up --build -d --wait
```

ตั้งค่า MCP client ให้ใช้ **Streamable HTTP** ที่ `http://127.0.0.1:8000/mcp` เซิร์ฟเวอร์ใช้ JSON responses แบบ stateless และเผยแพร่พอร์ตบน loopback สำหรับใช้งานในเครื่อง

ทดสอบ handshake จาก PowerShell

```powershell
$headers = @{ Accept = 'application/json, text/event-stream' }
$request = @{
    jsonrpc = '2.0'
    id = 1
    method = 'initialize'
    params = @{
        protocolVersion = '2025-06-18'
        capabilities = @{}
        clientInfo = @{ name = 'smoke-test'; version = '1.0.0' }
    }
}
Invoke-RestMethod -Uri http://127.0.0.1:8000/mcp -Method Post `
    -Headers $headers -ContentType application/json -Body ($request | ConvertTo-Json -Depth 8)
```

ตรวจสถานะด้วย `GET /healthz` และหยุดด้วยคำสั่ง

```powershell
docker compose down
```

## เครื่องมือ

| ชื่อ | การทำงาน |
| --- | --- |
| `usgs_search_earthquakes` | ค้นหาตามช่วงเวลา ขนาดแผ่นดินไหว และรัศมี |
| `tmd_get_daily_earthquakes` | ดึงเหตุการณ์ในวันที่ระบุ ค่าเริ่มต้นคือวันที่ปัจจุบันตามเวลาใน container ซึ่งตั้งเป็นเวลาไทย |
| `tmd_get_recent_earthquakes` | ดึงข้อมูลย้อนหลัง 1–7 วัน รวมวันนี้ ตัดเหตุการณ์ซ้ำและเรียงจากใหม่ไปเก่า |
| `match_events_to_assets` | จับคู่เหตุการณ์กับทรัพย์สินตามระยะทางจากจุดเหนือศูนย์เกิดแผ่นดินไหวและเกณฑ์ขนาด |
| `compute_parametric_payout` | คำนวณยอดจ่ายรายทรัพย์สินและยอดรวมตามเกณฑ์ขนาด ระยะทาง และทุนประกัน |

ผลลัพธ์อยู่ใน `structuredContent` และมี JSON ข้อความใน `content` สำหรับ client ที่อ่านผลแบบข้อความ เมื่อ TMD ส่ง `DailyEarthquakes` เซิร์ฟเวอร์จะกรองตามวันที่ไทยและสร้าง ID จากเวลา UTC กับพิกัดสำหรับรายการที่ไม่มี `EventID` ผลลัพธ์ขึ้นอยู่กับช่วงข้อมูลที่ TMD ส่งกลับมา
