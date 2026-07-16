# 실사용 / 배포 런북

> **지금 당장 실거래로 넘어가면 안 된다.** 아래 순서를 지킨다. 각 단계는 앞 단계가
> 끝나야 의미가 있다.

---

## 0. 현재 위치

| 단계 | 상태 |
|---|---|
| 코드 골격 + 안전장치 + 판단 + 자동화 | ✅ 완료 |
| 지표 엔진 | ✅ 완료 |
| 스크리너 게이팅 / 프롬프트 개편 / 메모리 3분할 / 주간 회고 | 🔨 작업 중 |
| **dry-run 1~2주** | ⬜ 다음 |
| 업비트 키 발급 + VPS | ⬜ |
| 최소금액 실거래 1회 | ⬜ |
| 본가동 | ⬜ |

---

## 1. dry-run (로컬, 1~2주) — 지금 할 것

업비트 키 없이 이 PC에서 돌린다. 공개 시세만 쓰므로 키가 필요 없고, 주문은 모의 체결된다.

```bash
# .env 에 DRY_RUN=true, ANTHROPIC_API_KEY만 있으면 된다
.venv/Scripts/python.exe main.py
```

- 대시보드: http://127.0.0.1:8000
- PC를 켜둔 동안만 돈다. 꺼지면 멈추지만 dry-run이라 상관없다.
- **이 기간에 봐야 할 것**: 판단 근거가 납득되는가 / 과매매하지 않는가 / 손절이 실제로 걸리는가 / 주간 평가가 제대로 도는가 / 월 API 비용이 예상 범위인가.
- 여기서 성과가 안 나오면 실거래로 넘어갈 이유가 없다. **dry-run 성과가 실거래 성과의 상한이다** (실거래는 슬리피지·체결지연이 더 나쁘다).

---

## 2. 업비트 API 키 발급

**선행 조건**: 업비트 계정 + 실명확인 입출금 계좌 등록 (없으면 Open API 신청 자체가 안 된다).

업비트 → 고객센터 → Open API 관리:

| 항목 | 설정 | 이유 |
|---|---|---|
| 자산조회 | ✅ 체크 | 잔고 확인에 필요 |
| 주문조회 | ✅ 체크 | 체결 확인에 필요 |
| 주문하기 | ✅ 체크 | 매매에 필요 |
| **출금하기** | ❌ **절대 체크 금지** | 이 프로그램의 안전 전제. 키가 유출돼도 돈이 밖으로 못 나간다 |
| 허용 IP | VPS의 고정 IP | **필수 입력** — 업비트가 강제한다 |

> ⚠️ **허용 IP 때문에 고정 IP가 사실상 필수다.** 가정용 인터넷은 IP가 바뀌면 봇이 그 순간
> 죽는다. 그래서 3단계(VPS)가 2단계보다 먼저 준비되어야 한다 — VPS를 먼저 띄우고 그
> IP로 키를 발급하는 순서가 맞다.

발급된 secret key는 **한 번만 보여준다.** 놓치면 재발급해야 한다.

---

## 3. VPS

서울 리전이어야 한다 (업비트 API 레이턴시).

| 선택지 | 비용 | 비고 |
|---|---|---|
| **Oracle Cloud 무료 티어** | **$0** | 권장. ARM 4코어/24GB까지 평생 무료. 춘천 리전 있음. 가입이 까다로운 편 |
| Vultr / Lightsail | $5~6/월 | 간단함. 서울 리전 있음 |

### 설치

```bash
sudo apt update && sudo apt install -y python3.10-venv git
git clone https://github.com/WOOHEEJUN/ai-trader.git && cd ai-trader
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env   # 키 입력, DRY_RUN=true 유지
```

### systemd 등록 (재부팅 자동 시작)

`/etc/systemd/system/ai-trader.service`:

```ini
[Unit]
Description=AI Trader
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ai-trader
ExecStart=/home/ubuntu/ai-trader/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ai-trader
sudo journalctl -u ai-trader -f    # 로그 확인
```

### 대시보드 접근 (Tailscale)

공인 인터넷에 절대 노출하지 않는다. VPS와 폰에 Tailscale을 깔면 끝 — 인증 코드를
직접 짤 필요가 없다.

```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
```

이후 폰에서 `http://<tailscale-ip>:8000` → 홈 화면에 추가하면 앱처럼 쓸 수 있다.
`.env`의 `WEB_HOST=0.0.0.0`으로 바꿔야 Tailscale 인터페이스에서 접근된다.

---

## 4. 최소금액 실거래 1회 (수동)

VPS에서 dry-run이 며칠 안정적으로 돈 뒤에만.

1. 업비트에 **5,000원만** 입금
2. `.env`에서 `DRY_RUN=false`
3. `sudo systemctl restart ai-trader`
4. 사이클 1회 수동 트리거: `.venv/bin/python -m agent.cycle`
5. **3중 확인**: 업비트 앱의 실제 체결 내역 / 대시보드 거래 내역 / `logs/`의 주문 로그가 일치하는가
6. 안 맞으면 즉시 `DRY_RUN=true`로 되돌리고 원인부터 찾는다

---

## 5. 서킷브레이커 강제 검증

본가동 전 마지막 관문. 손절이 실제로 주문을 내는지 확인한다.

```bash
# 감시 엔진이 -15%를 인식하도록 기준 스냅샷을 인위로 높여 넣는다
.venv/bin/python -c "
from state.store import get_store
from state.store import now_kst
s = get_store()
midnight = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
s.record_snapshot(999_999, 999_999, {}, ts=midnight.isoformat())
print('기준선 주입 완료 — 다음 감시 주기(60초)에 서킷브레이커가 발동해야 한다')
"
sudo journalctl -u ai-trader -f   # 60초 안에 '서킷브레이커 발동' 로그 확인
```

확인 후 주입한 스냅샷을 지우고 `runtime_state`의 `circuit_breaker_date`도 비운다.

---

## 6. 본가동

1. 10만원 입금
2. `DRY_RUN=false` 확인
3. `sudo systemctl restart ai-trader`
4. 첫 주는 매일 대시보드를 본다

---

## 실비용 — 냉정하게

| 항목 | 월 비용 |
|---|---|
| Anthropic API (스크리너 게이팅 기준) | $5~8 (≈7,000~11,000원) |
| VPS (Oracle 무료 티어면 $0) | $0~6 |
| **합계** | **월 7,000~2만원** |

**원금 10만원 대비 월 7~20%다.** 봇이 그만큼 벌어야 운영비가 상쇄된다 — 단타 수익률로
쉬운 수치가 아니다. 선택지는 셋 중 하나다:

1. **API 비용을 실험 참가비로 본다** — 주간 평가는 포트폴리오 평가액만 보므로 API 비용은
   손익에 안 잡힌다. "AI가 스스로 돈을 굴리는 걸 구경하는 값"으로 치는 것. 가장 정직한 태도.
2. **원금을 올린다** — 30만원이면 운영비 비중이 2~7%로 떨어져 수익률 해석이 의미 있어진다.
3. **Oracle 무료 티어 + 스크리너 게이팅으로 최소화** — 월 7,000원까지 낮출 수 있다.

원금 10만원으로 유지하겠다면 1번을 권한다. 성과 평가에서 API 비용을 빼고 보되,
"이 실험의 참가비는 월 1만원"이라고 인정하고 시작하는 게 맞다.

---

## 사고 시 대처

| 상황 | 대처 |
|---|---|
| 즉시 매매 중단 | `sudo systemctl stop ai-trader` — 포지션은 그대로 남는다 |
| 전량 청산하고 중단 | 업비트 앱에서 직접 매도 후 서비스 중단. 봇을 거치지 않는다 |
| 키 유출 의심 | 업비트에서 즉시 키 삭제 → 출금 권한이 없으므로 자금 유출은 불가 |
| 봇이 이상 매매 | `DRY_RUN=true`로 바꾸고 재시작하면 주문이 안 나간다 |

---

## 다시 강조

- 실제 돈이고 **전액 손실 가능**하다. 크립토 변동성은 서킷브레이커로 줄일 뿐 없앨 수 없다.
- 이건 **사용자 소유 봇**이다. 사용자가 직접 실행·운용하며, Claude(어시스턴트)는 코드만 작성했다.
- 투자 자문이 아니다. 순수 실험이다.
