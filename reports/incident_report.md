# Báo cáo sự cố — Doanh thu bị thổi phồng do trùng khóa đơn hàng

**Mã sự cố:** GAMEDAY-27-001
**Ngày:** 03/09/2026
**Nhóm:** Data/AI Reliability
**Trạng thái:** Đã xử lý xong

---

## Mức độ

Sự cố được xếp mức P2. Sai số tuyệt đối nhỏ, chỉ 1,58% doanh thu, và không có hệ thống nào phục vụ khách hàng bị gián đoạn nên chưa tới mức P1. Nhưng con số sai đi thẳng lên dashboard doanh thu của CEO, tức là một nơi dùng để ra quyết định, và pipeline vẫn báo `SUCCESS` suốt quá trình. Người đọc sẽ tin con số đó thay vì nghi ngờ nó, nên mức P3 là quá nhẹ.

## Tóm tắt

Lô dữ liệu `orders` ngày 03/09/2026 có 3 đơn hàng bị lặp, khiến tổng số dòng tăng từ 600 lên 603. Ba khóa `order_id` là 100000, 100001 và 100002, mỗi khóa xuất hiện hai lần. Các dòng lặp đều hợp lệ nếu xét riêng từng dòng: đúng kiểu dữ liệu, đúng loại tiền tệ, trạng thái hợp lệ, số tiền dương và thời gian còn mới. Chỉ có tính duy nhất của khóa chính bị vi phạm.

Hậu quả là doanh thu của các đơn đã hoàn tất bị cộng thừa 300,01 USD, tương đương 1,58%. Hệ thống báo 19.261,05 USD trong khi con số đúng là 18.961,04 USD.

Điểm quan trọng nhất của sự cố này là pipeline chưa bao giờ báo lỗi. Số dòng bình thường, độ tươi bình thường, phân phối giá trị bình thường. Nếu không có ràng buộc khóa duy nhất, dữ liệu sai sẽ lên tới dashboard mà không ai biết.

## Phát hiện

Sự cố bị bắt bởi kiểm tra `unique` trên cột `orders.order_id` trong tầng contract, mức `critical`, hành động `block`. Thời điểm phát hiện là 06:07 UTC ngày 03/09/2026, ngay lần chạy pipeline đầu tiên sau khi lô dữ liệu về. Hai tầng khác xác nhận độc lập: checkpoint của Great Expectations và data test `unique_stg_orders_order_id` của dbt.

Đáng chú ý hơn là những tầng đã không phát hiện được. Bộ phát hiện bất thường về khối lượng coi 603 dòng là hoàn toàn bình thường, với điểm số 0,71 so với mức kỳ vọng khoảng 622 dòng cho một ngày thứ Tư. Ba dòng thừa trên tổng 600 chỉ là 0,5%, không ngưỡng thống kê hợp lý nào bắt được mức chênh đó. Kiểm tra độ tươi cũng không thấy gì vì các dòng lặp mang đúng dấu thời gian mới. Kiểm tra phân phối bằng KS và PSI im lặng vì nhân đôi 3 trên 600 dòng gần như không làm dịch chuyển phân phối. Các kiểm tra `not_null`, `accepted_values` và `range` đều đạt vì từng dòng lặp đều hợp lệ. Ngay cả test `unique` trên `order_date` ở tầng mart cũng đạt, do mệnh đề `GROUP BY` gộp các dòng lặp về cùng một dòng theo ngày.

Từ đây rút ra bài học chính của bài thực hành. Phương pháp thống kê không thể bắt một vi phạm xác định có quy mô nhỏ. Mức chênh 0,5% sẽ không bao giờ vượt ngưỡng cảnh báo, dù nó sai rõ ràng. Ngược lại, một luật xác định cũng không thể bắt được kịch bản sụt khối lượng. Các tầng kiểm tra không dư thừa; mỗi tầng là thứ duy nhất nhìn thấy loại lỗi của riêng nó.

## Nguyên nhân gốc

Nguyên nhân là job trích xuất chạy lại và ghi thêm thay vì ghi đè theo khóa. Bốn dấu hiệu cùng chỉ về kết luận này.

Thứ nhất, các dòng lặp là bản sao nguyên vẹn của nhau, giống nhau ở mọi cột kể cả `created_at` và `updated_at`. Nếu là nghiệp vụ thật, tức khách đặt hàng hai lần, thì `order_id` phải khác và dấu thời gian cũng phải khác.

Thứ hai, ba khóa bị lặp là ba khóa nhỏ nhất trong bảng, từ 100000 đến 100002. Đây là phần đầu của bảng nguồn, dấu hiệu của một lần đọc lại bắt đầu từ đầu bảng thay vì từ mốc watermark.

Thứ ba, không có khóa nào bị thiếu và cũng không có khóa nào mới xuất hiện. Lô dữ liệu là tập đúng và đầy đủ, cộng thêm một phần đầu bị đọc lại. Điều này loại trừ khả năng nạp thiếu hoặc trộn nhầm với nguồn khác.

Thứ tư, lược đồ không đổi, nên không liên quan tới thay đổi hợp đồng từ phía hệ thống nguồn.

Nguyên nhân sâu xa là bước nạp dữ liệu không có ràng buộc khóa chính và không đảm bảo tính idempotent. Vì vậy mọi lần chạy lại, bất kể vì lý do gì, đều âm thầm nhân đôi dữ liệu.

## Bằng chứng

**1. Contract validator** (`python scripts/run_baseline.py`):

```text
contract action          : BLOCK
contract failed checks   : 1 (critical=1)
    [critical] unique on order_id: duplicate_rows=6
quarantined rows         : 6 -> reports/quarantine/orders_quarantine.csv
RUN STATUS: FAILED - downstream consumers must not use this batch.
```

Con số 6 là 3 khóa nhân 2 bản. Cả hai bản đều bị cách ly vì validator không có cơ sở để biết bản nào mới là bản đúng.

**2. dbt build** (`make dbt`) — tầng biến đổi từ chối chạy tiếp:

```text
20 of 29 FAIL 3 unique_stg_orders_order_id ......... [FAIL 3 in 0.02s]
Done. PASS=19 WARN=0 ERROR=1 SKIP=9 NO-OP=0 TOTAL=29
```

Chín node phía sau bị bỏ qua, trong đó có `fct_daily_revenue`. Mart chưa bao giờ được dựng từ dữ liệu hỏng; lỗi được chặn ngay ở tầng staging.

**3. Great Expectations** (`make gx`) — xác nhận độc lập kèm định tuyến theo mức độ:

```text
checkpoint success : False
failed expectations: 1 (critical=1)
  [critical] expect_column_values_to_be_unique on order_id
pipeline action    : BLOCK
quarantined rows   : 6
```

**4. Báo cáo triage** (`python scripts/triage.py`) — khoanh vùng vị trí lỗi:

```text
## 2. Volume
- incoming rows : 603
- baseline rows : 600
- change        : +3 (+0.5%)
- vs 43-day same-weekday baseline: ok (score=0.71, expected~622)

## 3. Keys
- duplicate `order_id` values: 3
- example duplicated keys : [100000, 100001, 100002]
- keys present in baseline but missing now: 0

Signals that fired: keys, contract
```

Lược đồ không đổi. Số dòng tăng không đáng kể và bộ phát hiện bất thường không kêu, nhưng phần khóa chỉ thẳng ra ba khóa bị lặp và xác nhận không có khóa nào bị mất.

**5. Tác động kinh doanh** — đối chiếu mart với dữ liệu nguồn:

```text
doanh thu kèm dòng lặp : 19.261,05
doanh thu sau khử lặp  : 18.961,04
chênh lệch             :    +300,01  (+1,58%)
```

## Phạm vi ảnh hưởng

Phạm vi được tính từ `data/baseline/lineage_graph.json` chứ không dựa vào trí nhớ.

```text
raw_orders (nơi khóa trùng phát sinh)
└── stg_orders                     ← contract và dbt test CHẶN tại đây
    └── fct_daily_revenue          ← lẽ ra đã bị thổi phồng (dbt đã bỏ qua)
        └── ceo_revenue_dashboard  ← con số sai mà CEO nhìn thấy (đã được bảo vệ)
```

Ở mức cột, đường đi là `raw_orders.amount` → `stg_orders.amount_usd` → `fct_daily_revenue.daily_revenue` → `ceo_revenue_dashboard.revenue`.

Hai tập dữ liệu bị ảnh hưởng là `fct_daily_revenue` và `ceo_revenue_dashboard`, trong đó `ceo_revenue_dashboard` là node lá, tức nơi người dùng thực sự đọc số. Cả hai đều nằm trong nhóm tài sản quan trọng với nghiệp vụ.

Nhánh RAG hoàn toàn không bị ảnh hưởng. Chuỗi `kb_documents → kb_active_docs → rag_index → support_agent` không phụ thuộc vào `orders`, nên Support Agent không cần xử lý gì. Chính lineage cho phép khẳng định điều này ngay thay vì mất công đi kiểm tra.

Tác động thực tế tới khách hàng và nghiệp vụ là không có, vì lệnh chặn giữ được ở tầng staging và không hệ thống nào phía sau đọc phải con số sai.

## Xử lý

Lô dữ liệu bị chặn ngay khi phát hiện. Hàm `enforce_contract` trả về `action=block` và `run_baseline.py` thoát với mã khác 0, nên không có gì lan xuống phía dưới.

Sáu dòng vi phạm được cách ly sang `reports/quarantine/orders_quarantine.csv` để kiểm tra lại, thay vì xóa bỏ.

Mart được giữ nguyên. `fct_daily_revenue` vẫn ở bản dựng tốt gần nhất, nên dashboard hiển thị số cũ nhưng đúng, thay vì số mới nhưng sai. Với một chỉ số tài chính thì đây là đánh đổi hợp lý.

Sau đó dữ liệu được khôi phục bằng cách khử trùng lặp theo khóa chính rồi chạy lại pipeline.

## Khôi phục

```bash
make reset      # lấy lại lô sạch (thực tế: trích xuất lại với watermark đúng)
make baseline   # contract + anomaly + lineage + SLO
make dbt        # dựng lại model kèm test
```

Trạng thái sau khôi phục:

```text
contract action          : PASS
contract failed checks   : 0 (critical=0)
row-count anomaly        : ok (auto:mad, score=0.83, baseline_median=622)
RUN STATUS: HEALTHY

dbt: Done. PASS=29 WARN=0 ERROR=0 SKIP=0 TOTAL=29
```

## Kiểm chứng

- [x] Contract sạch — `run_baseline.py` báo `action=PASS`, 0 kiểm tra thất bại, mã thoát 0.
- [x] dbt test sạch — `PASS=29 ERROR=0 SKIP=0`, gồm cả singular test `assert_revenue_matches_orders` đối chiếu mart với `stg_orders` theo từng ngày.
- [x] Chỉ số bất thường trở lại vùng bình thường — 600 dòng so với trung vị 622 của các ngày thứ Tư trước đó, điểm số 0,83 trên ngưỡng 3,5.
- [x] SLO ổn định — cả ba SLI đều có burn rate 0,0, chính sách `multiwindow_burn` ở mức `healthy` và `page=False`.
- [x] Đầu ra phía sau đã được đối chiếu — `assert_revenue_matches_orders` trả về 0 dòng, nghĩa là `fct_daily_revenue.daily_revenue` khớp chính xác tổng số tiền các đơn hoàn tất trong `stg_orders`. Doanh thu là 18.961,04 USD, đúng với dữ liệu nguồn.

Mục cuối là mục quan trọng nhất. Xác nhận rằng các test đã đạt không đồng nghĩa với xác nhận rằng con số là đúng. Singular test mới là thứ kiểm tra con số.

## Phòng ngừa

| Hành động | Người phụ trách | Hạn | Lý do |
|---|---|---|---|
| Thêm ràng buộc khóa chính hoặc dùng `MERGE` idempotent cho bước nạp orders | data-platform | 10/09/2026 | Xóa hẳn loại lỗi này thay vì phát hiện sau khi đã xảy ra. Đây là mục duy nhất thực sự xử lý nguyên nhân gốc. |
| Giữ `unique(order_id)` ở mức critical và hành động block | commerce-data | xong | Đây là tầng duy nhất nhìn thấy sự cố lần này, không được hạ xuống mức cảnh báo. |
| Trích xuất theo watermark thay vì đọc lại toàn bảng | data-platform | 17/09/2026 | Việc chạy lại từ dòng đầu tiên chính là thứ tạo ra phần dữ liệu lặp. |
| Cảnh báo khi file quarantine không rỗng | data-platform | 10/09/2026 | Hiện các dòng bị cách ly vẫn được ghi ra nhưng không ai được báo. Cách ly mà không thông báo mới chỉ là nửa biện pháp. |
| Thêm test đối chiếu nguồn với mart cho mọi mart tài chính | analytics-eng | 24/09/2026 | `assert_revenue_matches_orders` bắt được loại lỗi mà `unique` và `not_null` về bản chất không thể thấy. |
| Giữ unit test `scd_fanout_does_not_inflate_revenue` | analytics-eng | xong | Bảo vệ một lỗi tiềm ẩn mà dữ liệu sạch hiện tại đang che đi. Xem phần dưới. |

### Một lỗi tiềm ẩn phát hiện thêm trong quá trình điều tra

Khi truy ngược các con đường có thể làm doanh thu bị thổi phồng, chúng tôi phát hiện `fct_daily_revenue` join với `stg_customers where is_active = true` mà không khử trùng lặp chiều khách hàng. Một chiều SCD-2 hoàn toàn có thể có nhiều hơn một dòng active cho cùng một khách hàng, chẳng hạn khi bản cập nhật về muộn và không đóng bản cũ. Khi đó phép join sẽ nhân bản các dòng đơn hàng và làm doanh thu tăng lên, mà SQL không báo lỗi và data test cũng không fail.

Dữ liệu khách hàng hiện tại có đúng một bản active cho mỗi khách, cụ thể là 75 dòng active trên 75 khách hàng khác nhau. Vì vậy mọi data test đều đạt và lỗi hoàn toàn vô hình. Đây là một quả mìn đang chờ, không phải một tình huống lý thuyết.

Lỗi đã được sửa bằng cách gộp chiều khách hàng về một dòng cho mỗi khách trước khi join, và được cố định bằng một unit test của dbt với dữ liệu mẫu gồm hai bản active. Cách kiểm chứng là hoàn tác bản sửa rồi chạy lại: unit test fail với `FAIL 1` trong khi cả 22 data test vẫn đạt. Đó chính là lý do unit test không hề trùng lặp với data test.

## Phụ lục — Hiểu hệ thống ở Phase 0

Có hai tập dữ liệu quan trọng, nằm trên hai nhánh độc lập. `orders` phục vụ báo cáo doanh thu nên lỗi ở đây là lỗi tài chính. `kb_documents` phục vụ Support Agent dùng RAG nên lỗi ở đây tác động trực tiếp tới khách hàng, và nguy hiểm ở chỗ agent sẽ trả lời sai một cách rất tự tin.

Hai chuỗi phụ thuộc tương ứng là `orders → stg_orders → fct_daily_revenue → ceo_revenue_dashboard` và `kb_documents → kb_active_docs → rag_index → support_agent`.

Không có chỉ số đơn lẻ nào cho biết dữ liệu không đáng tin, và đó chính là điểm mấu chốt. `critical_contract_failures` bắt các vi phạm xác định. Chỉ số bất thường `row_count` so theo cùng ngày trong tuần bắt các sai lệch thống kê. `freshness_minutes` và tuổi của KB bắt dữ liệu hợp lệ nhưng đã quá cũ. Burn rate của SLO quyết định xem sự việc có đáng đánh thức người trực hay không. Sự cố lần này chỉ bị bắt bởi chỉ số đầu tiên và lọt qua tất cả các chỉ số còn lại.
