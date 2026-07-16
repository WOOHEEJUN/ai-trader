# AI 자율 암호화폐 매매 실험

Claude에게 자금을 맡기고 업비트 KRW 마켓에서 자율 매매시킨 뒤, 매주 성과를 평가하는 실험.
성공하면 전략 메모리가 보존되고 권한이 확대되며, 실패하면 메모리가 초기화("kill")된다.

설계 배경과 의사결정 근거는 [plan.md](plan.md) 참고.

---

## ⚠️ 먼저 읽을 것

- **실제 돈이며 전액 손실 가능성이 있다.** 크립토는 변동성이 매우 크다.
- **이건 투자 자문이 아니다.** 순수 실험/사이드 프로젝트다.
- **업비트 API 키는 반드시 "자산조회 + 주문" 권한만 발급하고 출금 권한은 주지 않는다.**
  이 프로그램은 그 전제 위에 설계됐다.
- 이 봇은 **사용자가 직접 실행·운용하는 사용자 소유 봇**이다.
- **`DRY_RUN=true`를 먼저 1~2주 돌려라.** 실거래 전환은 그다음이다.

## 설치

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
cp .env.example .env    # 그리고 ANTHROPIC_API_KEY 입력
```

Python 3.10+. 업비트 키는 dry-run 단계에서 필요 없다 (공개 시세 API만 사용).

## 실행

```bash
python main.py              # 스케줄러 + 대시보드 (http://127.0.0.1:8000)
python main.py --no-web     # 스케줄러만
python -m agent.cycle       # 사이클 1회 수동 실행
python -m pytest            # 테스트
```

## 구조

```
config.py            모든 한도값. 튜닝은 여기서만 한다
state/store.py       SQLite(거래·스냅샷·포지션·API사용량·평가·사이클) + strategy_memory.md
exchange/            업비트 REST. UpbitBroker(실주문) / PaperBroker(모의 체결)
agent/
  watchdog.py        손절·익절·트레일링·서킷브레이커 — LLM 미사용, 1분 주기
  budget.py          월 $10 예산, 3단계 감속
  strategist.py      Claude 호출 + 프롬프트
  executor.py        가드레일 7종 + 리밸런싱
  judge.py           주간 평가, 세대 관리 — LLM 미사용
  cycle.py           판단 → 실행 → 다음 시점 예약
scheduler.py         잡 4종
web/                 읽기 전용 대시보드 (FastAPI + PWA)
```

## 스케줄러 잡

| 잡 | 주기 | 하는 일 |
|---|---|---|
| `watchdog` | 60초 | 손절/익절/트레일링/서킷브레이커. **Claude 미호출 = 비용 0원** |
| `snapshot` | 60분 | 자산 스냅샷 (수익률·자산곡선·서킷브레이커 기준선) |
| `cycle` | 5분 틱 | 예정 시각이 지났으면 트레이딩 사이클 (Claude가 1~24시간 중 직접 지정) |
| `judge` | 월 09:00 | 주간 평가. **Claude 미호출** |

## 안전장치

**청산 규칙** (watchdog이 집행, LLM과 무관)

| 규칙 | 기본값 |
|---|---|
| 손절 | 진입 평단 −7% → 전량 매도 (Claude가 −1%~−10%에서 조정 가능) |
| 익절 | 진입 평단 +15% → **절반** 매도, 잔량은 트레일링으로 전환 |
| 트레일링 | 익절 후 고점 −5% → 잔량 전량 매도 |
| 서킷브레이커 | 당일 총평가액 −15% → 전 포지션 청산 + 당일 매매 중단 |

우선순위: 손절 > 트레일링 > 익절.

**가드레일** (executor가 집행)

- 거부: 유니버스 밖 / 평가 전 24h 쿨다운 중 매수 / 일일 매매 한도 / 서킷브레이커 발동일
- 조정: 단일 코인 50% 상한 / 1회 주문 30% 상한 / 비중 합 100% / 손절선 범위
- 생략: 목표-현재 비중 차 5%p 미만 / 최소주문금액 5,000원 미만

**API 예산**

| 소진율 | 상태 | 동작 |
|---|---|---|
| ~80% | normal | 정상 (effort=high, 최소 간격 1h) |
| 80~100% | throttled | effort=medium, 최소 간격 4h |
| 100%~ | suspended | **Claude 호출만 정지.** 청산 감시는 계속 작동. 매월 1일 자동 재개 |

> 예산이 소진돼도 포지션은 방치되지 않는다. `tests/test_safety_integration.py`가 이걸 강제한다.

## 실거래 전환

1. dry-run으로 1~2주 관찰 (대시보드에서 판단 품질 확인)
2. 서울 리전 VPS 준비 (**업비트는 API 키에 허용 IP 등록이 필수라 고정 IP가 필요하다**)
3. 업비트 키 발급 — **자산조회 + 주문만, 출금 권한 절대 금지**, VPS IP 등록
4. `.env`에 키 입력, `DRY_RUN=false`
5. `python -m agent.cycle`로 최소 금액 1회 트리거 → 체결·잔고·대시보드 반영 3중 확인
6. `python main.py`로 본 가동, systemd 등록

## 보안

- `.env`는 커밋 금지 (`.gitignore`에 있음)
- 대시보드는 읽기 전용이며 Tailscale 뒤에 두는 것을 권장 (공인 인터넷 미노출)
- 알림에 API 키를 절대 넣지 않는다
- VPS는 SSH 키 인증만 허용
