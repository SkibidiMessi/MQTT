HISTORICAL V2 DOCUMENTATION. MQTT topics/payload/availability instructions below are obsolete. Use README.md.

# Pi → MQTT → backend: telemetry và ưu tiên tác vụ

## Phạm vi

Mục tiêu là đọc dữ liệu Pixhawk và publish MQTT. Bộ này không nhận lệnh MQTT, không
arm/cất cánh/hạ cánh, không gửi setpoint, không upload nhiệm vụ và không ghi tham số PX4.
Điều khiển tư thế/tốc độ/động cơ vẫn do PX4 thực hiện. Navigation trên Pi ở đây chỉ là
ứng dụng điều phối/giao nhiệm vụ nếu dự án có; chưa có mã navigation trong bộ này.

MQTT Explorer là MQTT client để quan sát topic. Nó không phải broker và không tự
đưa dữ liệu lên website. Pi publish tới broker; MQTT Explorer và backend subscribe
cùng broker. Backend cần tích hợp subscriber rồi chuyển dữ liệu đến frontend.
Chưa có địa chỉ/credentials broker hoặc mã backend, nên bộ này không thể tự triển
khai lên backend của bạn. Ví dụ dưới dùng broker Mosquitto trên laptop để kiểm tra LAN.

## Bước 1 — broker thử nghiệm trên laptop Ubuntu

Các lệnh này chạy ở laptop, không phải Pi. Không cần Docker cho MQTT.

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients
mkdir -p ~/uav-mqtt-lab
cd ~/uav-mqtt-lab
touch passwords
mosquitto_passwd passwords uav_pi
```

Nhập mật khẩu mới khi được hỏi. Không ghi mật khẩu thật vào code hoặc gửi nó trong chat.
Tạo file `mosquitto-lab.conf` trong thư mục này, thay `/home/tran-long` nếu tài khoản khác:

```conf
listener 1884 0.0.0.0
allow_anonymous false
password_file /home/tran-long/uav-mqtt-lab/passwords
persistence false
```

Chạy broker foreground ở user hiện tại:

```bash
mosquitto -c ~/uav-mqtt-lab/mosquitto-lab.conf -v
```

Dùng cổng 1884 để không đụng listener 1883 có thể đang chạy sẵn. Giữ terminal này mở.
Nếu 1884 đã được dùng, chọn cổng khác và đổi đồng bộ phía Pi/Explorer/backend.
Đây là broker thử LAN có mật khẩu, chưa có TLS; không mở port này ra Internet.
Nếu dùng broker backend có sẵn, bỏ toàn bộ bước cài broker thử và dùng thông tin của nó.

Lấy IP laptop: `ip -4 addr show scope global`. Với IP đã dùng trước đây là
192.168.50.133, chỉ giữ nếu kiểm tra thấy vẫn đúng. Nếu UFW active chặn kết nối:

```bash
sudo ufw status
sudo ufw allow from IP_CUA_PI to any port 1884 proto tcp
```

Thay IP_CUA_PI bằng IP Pi; không chạy nguyên placeholder.

Trong MQTT Explorer trên laptop: Host `127.0.0.1`, Port `1884`, username `uav_pi`,
mật khẩu vừa đặt, SSL/TLS off cho broker thử này. Subscribe `uav/#`.
Nếu Explorer chạy trên máy khác, Host phải là IP laptop broker.

## Bước 2 — cài reader/publisher trên Pi

Giải nén thư mục thành `/home/pi/uav_gateway_v2`. Chạy trong SSH Pi:

```bash
cd ~/uav_gateway_v2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v test_reader.py test_mqtt.py
cp mqtt.example.json mqtt.local.json
nano mqtt.local.json
```

Đổi `host` thành IP broker mà Pi truy cập được, ví dụ 192.168.50.133; đổi `port`
thành 1884 cho broker thử. Username `uav_pi`. Không để host=127.0.0.1 nếu broker
ở laptop: trên Pi, 127.0.0.1 là chính Pi. Với broker thật có TLS, đặt `tls: true`,
đúng cổng và CA/client certificate nếu broker yêu cầu. Chương trình không tắt kiểm tra TLS.

## Bước 3 — router trên Pi

Chỉ MAVLink Router đọc USB. Dừng router cũ trước khi mở bản có các endpoint dưới.
Dừng ứng dụng đang đọc trực tiếp USB; ứng dụng khác cần endpoint UDP riêng.
Trong terminal SSH thứ nhất:

```bash
mavlink-routerd \
  -e 192.168.50.133:14550 \
  -e 127.0.0.1:14540 \
  /dev/serial/by-id/usb-Auterion_PX4_FMU_v6C.x_0-if00:115200
```

IP 14550 là laptop QGC, không bắt buộc là broker. 14540 dành riêng reader trên Pi.
Chỉ một process bind endpoint reader. Nếu systemd đã chạy router, chỉnh cấu hình
router đó thay vì mở thêm process. Thiết bị USB dùng đường dẫn thực tế trong
`ls -l /dev/serial/by-id/`; id autopilot mặc định 1/1, battery MAVLink id mặc định 0.

## Bước 4 — chạy gateway

Trong terminal SSH thứ hai:

```bash
cd ~/uav_gateway_v2
source .venv/bin/activate
read -rs -p 'MQTT password: ' MQTT_PASSWORD; echo
export MQTT_PASSWORD
python telemetry_reader.py \
  --connection udpin:127.0.0.1:14540 \
  --uav-id uav-01 \
  --mqtt-config mqtt.local.json \
  --request-streams \
  --output reports/status.json
```

`uav-01` là ví dụ ID, đổi theo dự án. Mỗi UAV dùng ID riêng và một publisher cho ID đó
(client id trùng sẽ đẩy nhau khỏi broker). Bỏ `--request-streams` nếu không muốn gửi
yêu cầu interval/home; reader vẫn gửi heartbeat nhận diện companion 245/191.
Thêm `--read-params` để đọc cấu hình PX4: kết quả ở `px4_parameters` trong file local,
không publish hàng nghìn tham số cấu hình lên MQTT mỗi giây.

Trong MQTT Explorer:

- `uav/uav-01/telemetry`: JSON 1 Hz, QoS 0, retain=false.
- `uav/uav-01/availability`: online/offline, retain=true, có Last Will.

Payload gồm `schema_version`, `session_id`, `telemetry` (58 trường của bảng),
`diagnostics` (tuổi bản tin, missing fields, bộ đếm publish/drop). Không phát giá trị
demo thay cho cảm biến thật. CPU/RAM/nhiệt độ/đĩa thuộc máy CHẠY code, có tên máy
ở `diagnostics.metrics_host`; nếu thử USB trên laptop chúng không phải số đo của Pi.

`mqtt_connected` xác nhận MQTT client Pi đã nhận CONNACK, không chứng minh backend
hoặc trình duyệt đã nhận/hiển thị bản tin. Backend cần theo dõi timestamp/sequence,
session_id và tuổi dữ liệu. Đề xuất đánh dấu mất cập nhật sau 5 giây, tùy yêu cầu hệ thống.
Availability là trạng thái ứng dụng, không phải tình trạng UAV hay flight readiness.
QoS0 có thể mất gói; dùng để cập nhật trạng thái mới nhất. Không dùng kênh này làm lệnh bay.
Khi mất mạng, bỏ snapshot mới thay vì tích lũy lịch sử; tự reconnect theo backoff.
Chỉ một telemetry publish pending. Gửi lại khi reconnect không đảm bảo exactly-once.

File `reports/status.json` ghi đè nguyên tử mỗi giây. `--jsonl` là tùy chọn ghi lịch sử
tăng mãi, không tự xoay log. Unit systemd không bật JSONL và không tải toàn bộ params
mỗi lần chạy để giảm lưu lượng. Ctrl+C thoát chương trình; SIGINT systemd cũng vậy.

## Giới hạn nguồn dữ liệu của bảng

Có bộ giải mã telemetry cho vị trí/tư thế/vận tốc/pin/GPS/home/landed/mode/armed.
Không phải mọi trường đều có thể đọc chỉ từ Pixhawk đang nằm trên bàn:

- Trường không biết/quá hạn là `null`; backend phải chấp nhận null, không thay thành 0.
- `failsafe_active`, `failsafe_reason` cần tích hợp trạng thái/sự kiện PX4 phù hợp;
  không suy từ một system_status hay dòng text. Phiên bản này để null.
- `home_position_valid` chưa có nguồn xác nhận đầy đủ nên để null dù nhận HOME_POSITION.
- HDOP/VDOP chỉ lấy khi có GPS_INPUT hợp lệ; không giả định GPS_RAW_INT.eph/epv của
  PX4 là DOP. Nếu PX4 không phát GPS_INPUT thì null.
- Các mục Mission (56–68) cần kế hoạch/backend và quy tắc mission item ↔ waypoint;
  phiên bản này chưa tích hợp, để null. Raw MISSION_CURRENT được giữ trong diagnostics local.
- `network_latency`, `packet_loss` chưa đo đường Pi→backend; 4G/5G cần thông tin modem.
- `mqtt_connected` nay đã có nguồn thật từ MQTT client.

`position_valid` là kiểm tra bảo thủ cho vị trí toàn cầu từ heartbeat, estimator flags
và độ mới dữ liệu; không phải kiểm tra đủ điều kiện bay. Tuổi tối đa đa số trường 3s,
GPS 5s, pin 10s, home 30s. GPS fix không tự chứng minh local position hợp lệ.
Nếu SYS_HITL vẫn bật mà không có simulator, nhiều trường sẽ không có dữ liệu.
Reader không sửa SYS_HITL hay bất kỳ parameter nào.

## Bước 5 — ưu tiên tác vụ trên Raspberry Pi OS

Tên đúng là OS task/process scheduling. Không cần đổi sang một OS mới chỉ để đặt ưu
tiên. Dùng Linux scheduler + systemd. Đây là ưu tiên best effort, không có deadline cứng.

| Mức | Tác vụ | Nice | CPUWeight | Vị trí |
|---|---|---:|---:|---|
| S1 | Navigation/điều phối nhiệm vụ | -5 | 900 | Pi, khi có chương trình thực tế |
| S1 hỗ trợ | MAVLink Router | -5 | 900 | Pi, đường giao tiếp dùng chung |
| S2 | Reader + MQTT telemetry | 0 | 100 | Pi |
| S3 | GStreamer/video | 10 | 10 | Pi |

Nice số nhỏ được ưu tiên hơn. CPUWeight áp dụng khi các service anh em cạnh tranh CPU
trong cùng `uav.slice`, không phải phần trăm CPU hoặc bảo đảm tỷ lệ tuyệt đối 900:100:10.
Khi rảnh, video vẫn có thể dùng CPU. Các tác vụ vẫn chạy đồng thời, không chờ S1 hoàn tất
mới chạy S2/S3. Network/GPU/encoder/USB không được ưu tiên tự động bởi Nice/CPUWeight.
Router phục vụ navigation nên không hạ nó xuống S3. Nhiệm vụ điều khiển bay vòng trong
chạy trên Pixhawk, không chịu Nice của Pi. Không dùng SCHED_FIFO/RR cho script Python
mạng/video khi chưa xác định deadline, giới hạn runtime và đo thử.

### Cài service S2

Chạy sau khi foreground đã đọc/publish thành công. Tắt reader foreground trước.
Đường dẫn và User trong unit giả định `/home/pi`, tài khoản `pi`; sửa nếu khác.

```bash
sudo install -d -m 700 /etc/uav
sudo install -m 600 telemetry.env.example /etc/uav/telemetry.env
sudo nano /etc/uav/telemetry.env
```

Điền host/user/password thật (host trong EnvironmentFile sẽ ưu tiên hơn JSON).
Chỉ thực hiện lệnh install mẫu một lần, không ghi đè file credentials đang dùng.

```bash
sudo install -m 644 systemd/uav.slice /etc/systemd/system/uav.slice
sudo install -m 644 systemd/uav-telemetry.service /etc/systemd/system/uav-telemetry.service
sudo systemctl daemon-reload
sudo systemctl enable --now uav-telemetry.service
journalctl -u uav-telemetry.service -f
```

Router phải được chạy riêng trước hoặc có service tự khởi động đúng cấu hình. Unit
không tự tạo router và không giả định tên service router. MemoryHigh=192M/MemoryMax=256M
là mốc khởi đầu cho reader, cần đo trên Pi, systemd có thể kill/restart reader nếu vượt.
Không dùng bản reader này làm chức năng navigation rồi áp nguyên giới hạn RAM đó.

### Áp ưu tiên vào service navigation/video/router thực tế

Các file `navigation-priority.conf`, `video-priority.conf`, `router-priority.conf`
chỉ là drop-in mẫu. Chúng không chứa ExecStart và không tự tạo ứng dụng.
Xác định tên unit thật, rồi dùng:

```bash
sudo systemctl edit TEN_SERVICE_THUC_TE.service
```

Dán nội dung drop-in tương ứng, lưu, rồi `sudo systemctl daemon-reload` và restart
service thực tế khi không có nhiệm vụ bay đang hoạt động. Các service phải cùng
Slice=uav.slice. Không paste placeholder TEN_SERVICE_THUC_TE thành service mới.
Nếu video hiện chạy gst-launch bằng terminal, cần tạo unit với ĐÚNG pipeline đang dùng
trước; bộ này không đoán camera/IP/port/video pipeline của bạn.

Kiểm tra:

```bash
systemctl show uav-telemetry.service -p Nice -p CPUWeight -p Slice -p MemoryCurrent
systemd-cgtop
ps -eo pid,ni,cls,pcpu,pmem,comm --sort=-pcpu | head -n 20
```

Chưa ghim CPU core hoặc giới hạn CPU video trước khi đo. Nếu cần deadline cứng cho
navigation, phải xác định chu kỳ, deadline, thời gian chạy xấu nhất và thử tải; Nice
không thay thế thiết kế real-time hay failsafe trên PX4.

## Bài kiểm chứng đề nghị

1. Pi chỉ chạy router + reader/MQTT: kiểm tra 58 keys, nguồn hợp lệ, timestamp tăng.
2. Mở MQTT Explorer cùng backend subscriber: xác nhận cả hai nhận cùng uav-id/sequence.
3. Ngắt Wi-Fi có kiểm soát trên bàn: reader vẫn đọc UART, không tăng hàng đợi vô hạn;
   broker báo offline theo keepalive; nối lại nhận trạng thái mới.
4. Bật video và navigation thật: đo khoảng cách thời gian giữa các vòng navigation,
   độ trễ telemetry đến backend, dropped sequence, CPU/RAM/nhiệt độ và FPS.
5. So sánh trước/sau đặt ưu tiên với cùng tải. Chỉ kết luận ưu tiên có tác dụng khi số
   đo cải thiện; chỉ số nice trong ps chưa chứng minh đạt deadline.

Không ngắt mạng hay restart service để thử tải trong chuyến bay thật.

## Kiểm tra đã thực hiện cho gói code

Kiểm tra offline chuyển đổi đơn vị/sentinel/enum/source/timeout và schema 58 trường.
Smoke test Paho MQTT thật với MQTT test peer TCP cục bộ: connect, publish JSON,
online/offline và bỏ snapshot khi mất kết nối/chưa gửi xong. Test peer không phải
broker Mosquitto đầy đủ; TLS, ACL, backend và scheduling thực tế cần kiểm tra trên Pi.
Đã kiểm tra thêm luồng tích hợp: phát bản tin MAVLink thật qua UDP vào process reader,
reader publish qua Paho TCP đến test peer và giữ đủ 58 trường. Khi không bật request
flags, các bản tin reader phát trong phép thử chỉ gồm HEARTBEAT.
Unit systemd đã kiểm tra cú pháp với ExecStart thay tạm bằng executable kiểm thử;
đường dẫn Python, user pi và dịch vụ thực tế chỉ tồn tại sau khi cài đúng trên Pi.

## Tài liệu

- https://mavlink.io/en/messages/common.html
- https://github.com/mavlink-router/mavlink-router
- https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html
- https://mosquitto.org/man/mosquitto-conf-5.html
- https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
- https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html
