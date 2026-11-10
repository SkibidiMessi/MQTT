# UAV Gateway 2.1.1 — MQTT theo cột CODE

## Thay đổi theo backend

- Gửi các trường telemetry bằng CODE trong bảng UAV_POC_Project(2).xlsx.
- Mặc định parameter_keys=code, không còn dùng tên trường làm khóa MQTT.
- Giữ nguyên các mã có số 0 ở đầu: 00, 01, 03...
- deviceID gồm 1–64 ký tự ASCII chữ/số. Mặc định uav01; không dùng IP,
  dấu chấm, gạch ngang, gạch dưới hoặc khoảng trắng.
- Bên web/backend phải chọn cùng deviceID, ví dụ uav01.
- Không thay đổi thuật toán đọc telemetry, đơn vị, hoặc cấu hình Pixhawk.
- Sửa tương thích LacPacket.Parse theo MQTT.md: timestamp đầu gói là ngày giờ UTC
  17 chữ số và thêm TYPE:1. Các trường CODE và ký tự TAB giữ nguyên.

## Topics

- lac/uav01/data: telemetry dạng TAB, QoS 0, retain=false, 1 bản tin/giây.
- lac/uav01/control: nhận lệnh backend, đăng ký QoS 1, tự đăng ký lại sau reconnect.
- lac/uav01/debug: JSON chẩn đoán, mặc định 10 giây/lần.
  Bảng không quy định payload debug; JSON là quy ước của ứng dụng này.
  Đặt debug_interval_s=0 nếu không cần bản tin debug định kỳ.

## Payload /data

```text
20261028145844221<TAB>TYPE:1<TAB>00:uav01<TAB>01:2.1.1<TAB>03:26<TAB>12:0<TAB>44:72<TAB>45:23.527
```

<TAB> ở ví dụ biểu diễn ký tự tab thực tế (0x09), không phải chuỗi chữ đó.
Payload UTF-8, không có JSON bao ngoài, không có newline ở cuối.
Timestamp đầu bản tin là ngày giờ UTC dạng yyyyMMddHHmmssfff (17 chữ số),
ví dụ 1793199524221 Unix ms được chuyển thành 20261028145844221.
TYPE:1 xác định đây là telemetry. Không thêm trường 02.
timestamp_ms trong reports/status.json và debug vẫn là Unix milliseconds.
Code 00=uav_id, 01=edge_app_version, 03=sequence_no, 12=armed,
44=battery_remaining (%), 45=battery_voltage (V).
Tất cả 58 ánh xạ CODE/tên trường đã được đối chiếu bảng mới.
Các trường không biết, quá hạn hoặc chưa triển khai được BỎ QUA, không gán 0.
Backend cần đánh dấu dữ liệu thiếu/quá hạn, không giữ giá trị cũ như thể còn mới.
Boolean dùng 0/1. Các giá trị số thực vẫn được có dấu chấm thập phân;
quy định không có dấu chấm chỉ áp dụng cho deviceID.
Quy ước dữ liệu thiếu giữ như bản trước: bỏ qua trường chưa có giá trị.

## Chạy laptop nối USB với Pixhawk

Đóng QGC / MAVLink Router nếu đang chiếm cùng cổng USB. Dừng gateway cũ bằng Ctrl+C.
Giải nén gói mới vào thư mục riêng để không chạy nhầm bản cũ. Mở terminal trong
thư mục uav_gateway_v2 rồi chạy:

```bash
bash run_laptop.sh uav01
```

Script chuẩn bị .venv, cài requirements, hỏi mật khẩu MQTT và chạy reader.
Không nhập IP làm ID. Muốn mã khác, ví dụ:

```bash
bash run_laptop.sh UAVLAB02
```

Cấu hình broker có sẵn ở mqtt.remote.json: mqtt.lacais.com:8883, TLS bật và kiểm tra
chứng chỉ. Mật khẩu lấy từ biến môi trường MQTT_PASSWORD, không lưu trong code.
Giá trị MQTT_HOST / MQTT_PORT / MQTT_USERNAME trong môi trường sẽ ghi đè config.
Không chạy hai gateway cùng deviceID một lúc vì chúng dùng cùng MQTT client ID.

Chạy thủ công nếu đã chuẩn bị .venv:

```bash
read -rsp 'Mat khau MQTT: ' MQTT_PASSWORD
echo
export MQTT_PASSWORD
.venv/bin/python -u telemetry_reader.py \
  --connection /dev/serial/by-id/usb-Auterion_PX4_FMU_v6C.x_0-if00 \
  --uav-id uav01 --request-streams \
  --mqtt-config mqtt.remote.json --output reports/status.json --quiet
```

Trên Pi dùng MAVLink Router làm tiến trình duy nhất đọc USB, thay connection bằng
udpin:127.0.0.1:14540 và thêm endpoint 127.0.0.1:14540 ở router.
File reports/status.json vẫn giữ tên trường và JSON để chẩn đoán tại máy.
Chỉ payload MQTT /data dùng mã CODE.

## Kiểm tra nhận lệnh

Mở MQTT Explorer, xem lac/uav01/control, bấm nút trên web.
Gateway in 10 lệnh đầu (sau đó mỗi lệnh thứ 100) và lưu mọi tin nhắn nhận được vào
reports/mqtt_control.log, tối đa 1 MiB/file, giữ 2 bản log xoay vòng.
Log có topic, payload, thời gian nhận, QoS, cờ retained, executed=false.
Payload >4096 byte hoặc UTF-8 không hợp lệ được ghi là lỗi đầu vào.

CHỈ THEO DÕI: chưa phân tích hay thực thi lệnh bay. Không gửi ARM, TAKEOFF, LAND,
OFFBOARD, PARAM_SET hoặc tải mission từ MQTT. Reader vẫn gửi companion heartbeat
và yêu cầu đọc/stream nếu bật --request-streams/--read-params như trước.
Cả tin nhắn retained cũng chỉ ghi log. Nhận tin không đồng nghĩa Pixhawk chấp nhận
hay hoàn thành lệnh. Chưa có giao thức trả kết quả điều khiển cho backend.

## Kiểm thử

```bash
.venv/bin/python -m unittest test_reader test_mqtt -v
```

Kiểm tra timestamp UTC 17 chữ số, TYPE:1, chuyển đổi telemetry, CODE mặc định,
số 0 ở đầu mã, TAB thực tế, dữ liệu thiếu,
ID chữ/số, Paho gửi/nhận với MQTT peer tại máy, ghi log retained control mà không
thực thi. Chưa kiểm thử với broker của nhóm hoặc phần cứng ở lần sửa này.
README_V2_OLD.md chỉ là tài liệu lịch sử; không dùng các topic/lệnh cũ trong đó.
