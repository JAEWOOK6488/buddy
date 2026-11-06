# buddy

Ubuntu 데스크톱에 사는 AI 버디예요. 노치에 질문하면 Claude가 화면을 보고 답하고, 소리 내어 읽어주고,
파란 커서가 날아가 해당 UI를 가리켜요. 시각화(3D·구조도)와 마우스·키보드 작업도 해요.

[Clicky](https://github.com/farzaa/clicky)(macOS)의 Linux 포트예요 — 출처는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## 요구 사항

- Ubuntu 22.04 x86_64, **X11 세션** (로그인 화면 톱니바퀴 → `Ubuntu on Xorg`). Wayland는 지원하지 않아요.
- Claude Pro/Max 구독 + [Claude Code](https://claude.com/claude-code) 로그인 (`claude`를 한 번 실행)
  — 또는 Anthropic API 키

## 설치

```bash
sudo apt install python3-venv libxcb-cursor0
git clone https://github.com/JAEWOOK6488/buddy && cd buddy
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
packaging/install-desktop.sh --autostart   # 앱 목록 등록 + 로그인 시 자동 실행
```

Super 키 → `buddy`로 실행해요. 제거는 `packaging/install-desktop.sh --uninstall`.
터미널에서 실행하려면 `.venv/bin/python -m buddy`.

## 단축키

| 단축키 | 동작 |
|---|---|
| `Ctrl+Alt+Space` | 노치 열기 → 입력창 포커스 → 접기 |
| `Ctrl+Alt` 누른 채 유지 | 가리키기 모드: 커서 밑 요소에 테두리, 떼면 설명 |
| `Ctrl+Alt` 누른 채 동그라미·밑줄 | 그린 영역 전체를 질문 |
| `Ctrl+Alt+Backspace` | 답변·작업 멈추기 (진행 중인 게 없으면 시각화 창 닫기) |

**노치 안**

| 키 | 동작 |
|---|---|
| `Enter` | 질문 보내기 / 승인을 기다리는 작업 단계 실행 |
| `Esc` | 답변 멈추기, 한 번 더 누르면 노치 접기 / 작업 중단 |

**시각화 창**: 드래그 = 회전, 휠 = 확대, 오른쪽 드래그 = 이동, `Esc` 또는 `× Clear` = 닫기

모델 변경·음성 끄기·대화 초기화·종료는 상단 바 트레이 아이콘 메뉴에 있어요.

## 기능

- **질문**: 모든 모니터 스크린샷과 함께 묻고, 답을 Edge TTS로 읽어줘요.
- **가리키기 모드**: 접근성(AT-SPI) → 창 경계 → Claude 보정 순으로 요소를 찾아요.
  Chrome·Electron 앱도 정확히 잡으려면 `gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
- **작업 시키기**: "PDF로 내보내줘"처럼 요청하면 끝날 때까지 스스로 진행해요(단계마다 화면을 다시 보고 다음 동작을 정해요).
  멈추려면 `Ctrl+Alt+Backspace`, 또는 마우스를 화면 모서리로 밀어두면 다음 단계 전에 멈춰요. 25단계를 넘으면 자동으로 멈춰요.
  **터미널에서 Enter로 명령을 실행하는 단계만** 노치에서 `Enter`로 승인을 받아요(`BUDDY_TASK_APPROVAL`로 변경).
- **시각화**: "시각화해줘", "3D로 보여줘"라고 하면 투명 창에 3D 모델·설명 카드·구조도를 띄워요.
  창은 격리돼 있어 인터넷·파일에 접근하지 못해요.

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `BUDDY_BACKEND` | `subscription` | `api`면 `ANTHROPIC_API_KEY` 사용 (사용량 과금) |
| `BUDDY_MODEL` | `claude-opus-5-5` | 트레이 메뉴에서도 변경 가능 |
| `BUDDY_NOTCH_HOTKEY` | `ctrl+alt+space` | 노치 단축키 |
| `BUDDY_STOP_HOTKEY` | `ctrl+alt+BackSpace` | 멈추기 단축키 |
| `BUDDY_POINT_MODE` | `true` | `false`면 Ctrl+Alt 가리키기 끔 |
| `BUDDY_TASK_APPROVAL` | `once` | 작업 승인: `once`=요청 한 번(터미널 명령 실행만 승인), `never`=승인 없음, `each`=단계마다 |
| `BUDDY_LANGUAGE` | (자동) | 답변 언어 고정, 예: `ko` |
| `BUDDY_SPEECH_ENABLED` | `true` | 읽어주기 |
| `BUDDY_VISUAL_GPU` | `auto` | NVIDIA PRIME 노트북에서 시각화 GPU (`auto`=내장 GPU, `nvidia`) |
| `BUDDY_CLAUDE_CLI` | (자동) | `claude` 경로를 못 찾을 때 지정 |

## 문제 해결

- 로그: `~/.local/state/buddy/buddy.log`
- 단축키가 안 먹으면 다른 앱이 같은 조합을 쓰는지 확인하고 `.env`에서 바꾸세요.
- 저장소 폴더를 옮겼다면 `packaging/install-desktop.sh`를 다시 실행하세요.

## 아직 없는 것

음성 입력(STT), Wayland 지원
