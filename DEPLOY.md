# 실사용 / 배포 런북

> **목표**: 24시간 가동, 어디서든 폰으로 확인. 서버비는 최소(월 4~5천 원대)로 — 무료가
> 아니어도 상관없다.
> **평가 기준**: API·서버 비용은 실험 참가비로 보고 손익에서 제외한다. 순수 투자 수익만 본다
> (주간 평가는 포트폴리오 평가액만 보므로 코드 변경 없이 이미 그렇게 동작한다).

---

## 0. 일정

서버를 처음부터 VPS(Vultr 권장, §3)에 올려두고, 8월엔 `DRY_RUN` 플래그만 바꾼다.
환경이 하나뿐이라 "내 PC에선 됐는데 서버에선 안 되네"가 생기지 않는다.

| 시기 | 할 일 | 업비트 키 | 서버비 |
|---|---|---|---|
| **지금** | Vultr 세팅 (§3) → dry-run 24시간 가동 | 불필요 | 월 ~$4 |
| **7월 내내** | 폰으로 지켜보며 전략·코드 수정 | 불필요 | 월 ~$4 |
| **7월 말** | 업비트 키 발급(서버 IP로) + 5,000원 실거래 검증 | 필요 | 월 ~$4 |
| **8월** | `DRY_RUN=false` → 본가동 | 필요 | 월 ~$4 |

## 현재 진행 상황

| 단계 | 상태 |
|---|---|
| 코드 골격 + 안전장치 + 판단 + 자동화 + 지표 엔진 | ✅ 완료 |
| 스크리너 게이팅 / 프롬프트 개편 / 메모리 3분할 / 주간 회고 | ✅ 완료 |
| **dry-run 개시 (VPS)** | ⬜ 다음 |
| VPS + 업비트 키 | ⬜ 7월 말 |
| 최소금액 실거래 1회 + 서킷브레이커 검증 | ⬜ 7월 말 |
| 본가동 | ⬜ 8월 |

---

## 1. dry-run (VPS, 7월 내내) — 서버 세팅(§3)을 먼저 하고 온다

업비트 키 없이 24시간 돌린다. 공개 시세만 쓰므로 키가 필요 없고, 주문은 모의 체결된다.
**이 단계에선 허용 IP 문제가 아예 없다** — 8월에 키를 넣을 때 비로소 생긴다.

```bash
# VPS에서
sudo systemctl enable --now ai-trader
sudo journalctl -u ai-trader -f
```

`.env`에는 `DRY_RUN=true`, `ANTHROPIC_API_KEY`, `WEB_HOST=0.0.0.0` 세 개면 된다.

- **이 기간에 봐야 할 것**: 판단 근거가 납득되는가 / 과매매하지 않는가 / **무신호 진입을
  안 하는가** / 손절이 실제로 걸리는가 / 주간 평가가 제대로 도는가 / API 비용이 예상 범위인가.
- 여기서 성과가 안 나오면 실거래로 넘어갈 이유가 없다. **dry-run 성과가 실거래 성과의
  상한이다** (실거래는 슬리피지·체결지연이 더 나쁘다).
- 코드를 고칠 땐 로컬에서 수정 → 커밋/푸시 → 서버에서 `git pull && sudo systemctl restart ai-trader`.

### 폰에서 보기 — Tailscale (무료, 서버 종류 무관)

대시보드 포트를 인터넷에 여는 대신 [Tailscale](https://tailscale.com)로만 접근한다.
개인용 무료(100대), 공인 인터넷 노출 없음, 인증 코드를 짤 필요가 없다.

```bash
# VPS에서
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
```

폰에 Tailscale 앱 설치 → 같은 계정 로그인 → 브라우저에서
`http://<인스턴스의 Tailscale IP>:8000` → **홈 화면에 추가**하면 앱처럼 쓴다 (PWA).

이러면 VPS 방화벽에 8000 포트를 열지 않아도 되고, 세상 누구도 대시보드에 접근할 수 없다.

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

## 3. 호스팅 — 유료 최소 비용 기준

> 무료(Oracle Always Free)를 원하면 이 섹션 하단의 대안 표를 본다. 유료를 감수하면
> 가입이 훨씬 간단하고 용량 부족("Out of host capacity") 문제도 없다.

### 왜 선택지가 좁은가

업비트가 **허용 IP 등록을 강제**한다. 그래서 필요한 건 "고정 공인 IP를 가진, 항상 켜져 있는
VM"이다. 이 조건이 흔한 서버리스/PaaS를 전부 탈락시킨다 (가격과 무관하게):

| 후보 | 왜 안 되는가 |
|---|---|
| Render / Railway / Fly.io | 유휴 시 슬립하거나 재기동마다 IP가 바뀜 → 화이트리스트 불가, 감시 잡이 죽는다 |
| GitHub Actions | IP가 매 실행마다 바뀜 + 최소 cron 5분이라 **1분 손절 감시 불가** + 상태 저장 불가 |
| PythonAnywhere | 아웃바운드가 화이트리스트 도메인으로 제한 — 업비트 호출 자체가 안 됨 |
| Vercel / Netlify 등 서버리스 | 상시 실행 프로세스 개념이 없음 |

**필요한 건 딱 하나 — 고정 IP가 있는, 24시간 켜져 있는 최소 사양 VPS.** 이 봇은 CPU를
거의 안 쓰고(파이썬 프로세스 1개 + SQLite) 트래픽도 본인만 쓰므로 가장 싼 플랜이면 충분하다.

### 월 최소 비용 선택지 (2026년 기준 시세 — 가입 시 최신 가격 확인)

| 선택지 | 월 비용 | 서울 리전 | 비고 |
|---|---|---|---|
| **Vultr — Cloud Compute 최소형** | **약 $3.5~6** (512MB~1GB) | **있음** | **권장.** 신용카드만 있으면 5분 가입, 즉시 발급. 고정 IP 기본 포함. 리전 선택 시 서울 지정 |
| DigitalOcean — Basic Droplet | 약 $4~6 (512MB~1GB) | 없음(싱가포르가 가장 가까움) | 가입·문서가 가장 쉬움. 업비트까지 ~40ms 정도 추가 지연, 시간 단위 매매엔 무해 |
| Linode(Akamai) — Nanode | 약 $5 (1GB) | 없음(도쿄가 가장 가까움) | 안정적. Vultr와 비슷한 포지션 |
| AWS Lightsail | 약 $5 (1GB) | **있음** | 고정 IP(정적 IP) 무료. AWS 콘솔이 다소 복잡 |
| Oracle Cloud Always Free | **$0** | 있음(춘천) | §3-1 참고. 가입 까다롭고 ARM 용량 부족이 잦음 |

**결론: Vultr Cloud Compute, 서울 리전, 최소 사양(512MB~1GB) 플랜.** 이유는 세 가지다 —
① 서울 리전이라 레이턴시가 가장 낮고 ② 가입이 가장 빠르고(신용카드만 있으면 즉시) ③ 고정 IP가
플랜에 기본 포함돼 별도 설정이 없다. 월 4~5천 원대다.

### Vultr 세팅

1. [vultr.com](https://www.vultr.com) 가입 → 결제수단 등록
2. Deploy New Server → **Cloud Compute (Shared CPU)**
3. Location: **Seoul**
4. Image: **Ubuntu 22.04 LTS**
5. Plan: 가장 저렴한 것 (512MB~1GB — 이 봇엔 충분하다)
6. SSH 키 등록 (없으면 콘솔에서 root 비밀번호로 접속 가능하지만 SSH 키 권장)
7. 배포 완료 후 인스턴스 상세 페이지에서 **IPv4 주소**를 확인 — 이게 업비트에 등록할 고정 IP다
8. 방화벽(Vultr 콘솔의 Firewall 또는 서버 내 `ufw`): 인바운드는 **SSH(22)만** 연다.
   대시보드 포트(8000)는 **절대 열지 않는다** — Tailscale로만 접근한다

```bash
ufw allow 22/tcp
ufw enable
```

### §3-1. 무료로 하고 싶다면 — Oracle Cloud Always Free

| 선택지 | 비용 | 리전 | 비고 |
|---|---|---|---|
| Oracle Cloud Always Free | **평생 $0** | 춘천/서울 | 고정 IP 무료, ARM 4코어/24GB 또는 AMD 1GB×2. 가입 시 해외결제 카드 필요, ARM은 용량 부족 잦음 |
| AWS Free Tier | 12개월 $0 → 이후 과금 | 서울 | 실험 기간(1년)은 커버됨 |
| GCP Always Free | 평생 $0 | 미국만 | 서울 리전 없어 레이턴시 손해, 시간 단위 매매엔 무해하지만 굳이 |

### Oracle 세팅

1. [oracle.com/cloud/free](https://www.oracle.com/kr/cloud/free/) 가입 — **해외결제 가능 카드 필요**
   ($1 가승인 후 취소됨. Always Free 범위 안에선 과금 없음)
2. 리전은 **춘천(ap-chuncheon-1)** 또는 서울 선택 — 가입 후 변경 불가하니 주의
3. Compute → Instance 생성
   - Shape: `VM.Standard.A1.Flex` (ARM, 4 OCPU / 24GB) — 안 되면 `VM.Standard.E2.1.Micro` (1GB)
   - Image: Ubuntu 22.04
   - SSH 키 저장 (다시 못 받는다)
4. Networking → **Reserved Public IP** 할당 (무료 1개). 이게 업비트에 등록할 고정 IP다
5. 방화벽: 인바운드는 **SSH(22)만** 연다. 대시보드 포트(8000)는 **절대 열지 않는다** —
   Tailscale로만 접근한다

> ⚠️ **ARM 인스턴스는 "Out of host capacity"가 자주 뜬다.** 인기가 많아서다. 몇 번 재시도하거나
> AMD Micro(1GB)로 간다. **우리 봇은 1GB로 충분하다** (Python 프로세스 하나 + SQLite).

> ⚠️ **Always Free 계정은 유휴 인스턴스 회수 정책이 있다** (7일간 CPU 사용률이 낮으면 회수).
> 우리 봇은 CPU를 거의 안 쓰므로 대상이 될 수 있다. **PAYG로 업그레이드하면 회수 대상에서
> 빠지고, Always Free 리소스는 계속 무료다.** 카드가 이미 등록돼 있으니 업그레이드만 하면 되고,
> Always Free 범위를 넘지 않는 한 청구액은 0원이다. 8월 실거래 전에 해두는 걸 권한다 —
> 실거래 중에 인스턴스가 회수되면 포지션이 방치된다.

### 설치 (Vultr/Oracle 등 Ubuntu 기반이면 공통)

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

대시보드 원격 접근(Tailscale)은 §1에서 이미 설정했다면 생략. 아직이면 §1 "폰에서 보기"를 본다.

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

## 실비용

| 항목 | 월 비용 |
|---|---|
| 서버 (Vultr 최소 사양, 서울 리전) | 약 $4~6 (≈5,500~8,500원) |
| 대시보드 원격 접근 (Tailscale 개인용) | **0원** |
| Anthropic API (스크리너 게이팅 기준) | $5~8 (≈7,000~11,000원) |
| **합계** | **월 약 12,500~19,500원** |

무료로 낮추고 싶으면 Oracle Always Free(§3-1)로 서버비를 0원으로 만들 수 있다 —
그러면 합계는 API 비용만 남아 월 7,000~11,000원이 된다.

**평가 기준**: API·서버 비용은 손익에서 제외하고 순수 투자 수익만 본다 (합의된 방침).
주간 평가는 포트폴리오 평가액만 비교하므로 코드 변경 없이 이미 그렇게 동작한다 —
이 비용들은 별도 결제이고 봇의 잔고를 건드리지 않는다.

즉 **월 1.3~2만원이 이 실험의 참가비**다.

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
