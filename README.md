# buddy

Ubuntu 데스크톱에 사는 AI 버디예요. 화면 상단 노치에 질문을 입력하면 모든 모니터를 보고
Claude가 답하고, 답을 소리 내어 읽어주고, 파란 삼각형 커서가 날아가서 해당 UI 요소를 가리켜요.

[Clicky](https://github.com/farzaa/clicky)(macOS)를 Linux로 옮긴 프로젝트예요. 출처와 라이선스는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 있어요.

## 지원 환경

- **Ubuntu 22.04, x86_64**
- **X11 세션 필수** — 로그인 화면 우측 하단 톱니바퀴에서 `Ubuntu on Xorg` 선택.
  Wayland에서는 전역 단축키·무음 스크린샷·커서 위치·오버레이 좌표 지정이 막혀 있어서 실행을 거부해요.
- Python 3.10 (22.04 기본)
- arm64는 PyQt6 휠이 glibc 2.39를 요구해서 22.04에서 설치되지 않아요.
- PyQt6는 6.11로 고정돼 있어요. 시각화 창에 쓰는 PyQt6-WebEngine에 6.12 휠이 없고, 두 패키지는 Qt 버전이 같아야 하기 때문이에요.
  WebEngine 포함 설치 용량은 약 500MB예요.

## 설치

```bash
sudo apt install python3-venv libxcb-cursor0
cd buddy
# --system-site-packages: uses Ubuntu's preinstalled python3-gi for accessibility (AT-SPI)
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Claude 연결 방식 (`.env`의 `BUDDY_BACKEND`)

| 값 | 쓰는 것 | 준비 |
|---|---|---|
| `subscription` (기본) | Claude Pro/Max 구독 | [Claude Code](https://claude.com/claude-code) 설치 후 `claude`를 한 번 실행해서 로그인. 키 불필요 |
| `api` | Anthropic API (사용량 과금) | platform.claude.com에서 키 발급 → `ANTHROPIC_API_KEY` 입력 |

`subscription`은 질문마다 `claude -p`를 헤드리스로 실행해요. 도구, MCP, 설정 파일, 세션 저장은 모두
끈 상태로 빈 임시 디렉토리에서 돌기 때문에, 평소 쓰는 Claude Code 설정이 buddy 답변에 섞이지 않아요.
대신 CLI가 매번 새로 떠서 첫 글자가 나오기까지 1~2초 더 걸리고, 구독 사용량 한도를 Claude Code와 같이 써요.

## 앱으로 설치 (Ubuntu 앱 목록에 추가)

```bash
packaging/install-desktop.sh              # 앱 목록(Super 키 → "buddy")에 추가
packaging/install-desktop.sh --autostart  # + 로그인하면 자동 실행
packaging/install-desktop.sh --uninstall  # 제거
```

- sudo 없이 현재 사용자에게만 설치돼요(`~/.local/share/applications`, `~/.config/autostart`).
  런처는 이 저장소의 `bin/buddy`를 가리키므로 **저장소를 옮기면 다시 설치**하세요.
- 앱 목록에서 아이콘을 우클릭 → `Add to Favorites`로 독에 고정할 수 있어요.
- 이미 실행 중일 때 아이콘을 다시 누르면 새로 뜨지 않고 노치가 열려요.
- 터미널이 없으니 로그는 `~/.local/state/buddy/buddy.log`(직전 실행은 `buddy.log.1`)에 남고,
  시작에 실패하면 데스크톱 알림으로 알려줘요.
- 앱 목록에서 실행하면 셸의 PATH가 적용되지 않을 수 있어서, `claude` CLI를 `~/.local/bin`, nvm, npm-global
  위치에서도 찾아요. 그래도 못 찾으면 `.env`에 `BUDDY_CLAUDE_CLI=/경로/claude`를 넣으세요.

## 터미널에서 실행

```bash
source .venv/bin/activate
python -m buddy
```

| 동작 | 방법 |
|---|---|
| 노치 열기 / 입력창 포커스 / 닫기 | `Ctrl+Alt+Space` (`.env`의 `BUDDY_NOTCH_HOTKEY`로 변경) |
| 질문 | 노치 입력창에 입력 후 Enter |
| **가리켜서 묻기** | **Ctrl+Alt를 누른 채** 커서를 대상 위에 두고 떼기. 누르고 있는 동안 커서 밑 요소에 파란 테두리가 따라다님 |
| 영역 묻기 | Ctrl+Alt를 누른 채 여러 요소를 **동그라미 치거나 밑줄 긋고** 떼기 (궤적은 잔상으로 남았다가 사라짐) |
| **답변 멈추기** | 어디서든 `Ctrl+Alt+Backspace`, 노치에서 `Esc`, 트레이 메뉴 `Stop answer`. 생성·읽기·가리키기·테두리를 모두 멈추고 대화 기록은 유지 |
| 노치 닫기 | Esc (답변 중이면 첫 Esc는 멈추기, 다음 Esc가 닫기) |
| 모델 변경·음성 끄기·버디 숨기기·대화 초기화·종료 | 상단 바 우측 트레이 아이콘 메뉴 |

## 작업 시키기 (마우스·키보드 조작)

"이 문서 PDF로 내보내줘"처럼 **해달라고** 하면 Claude가 판단해서 동작을 하나씩 제안해요.
"어떻게 해?"처럼 물으면 조작하지 않고 설명과 가리키기만 해요.

1. 노치에 `Step 1: click 'Export PDF'`처럼 다음 동작이 뜨고, 파란 버디가 그 위치로 날아가요.
2. **Enter = 실행**, **Esc = 작업 중단**, 다른 말을 입력하면 새 질문으로 바뀌어요.
3. 실행 후 새 화면을 보고 다음 동작을 제안해요. 끝나면 결과를 말해줘요.

| 동작 | 구현 |
|---|---|
| 클릭·더블클릭·우클릭·드래그·스크롤 | X11 XTEST |
| 키 조합 (`ctrl+s`, `Return` …) | X11 XTEST |
| 글자 입력 | 클립보드 붙여넣기 (한글 포함). 터미널 창은 Ctrl+Shift+V. 이전 클립보드는 1.5초 뒤 복구 |

안전장치:
- 모든 동작은 승인 후에만 실행돼요.
- `Ctrl+Alt+Backspace`나 Esc로 언제든 중단할 수 있어요.
- 승인할 때 마우스를 화면 모서리에 두면 실행하지 않고 중단해요.
- 25단계를 넘으면 자동으로 중단해요.
- 비밀번호, 결제, 되돌릴 수 없는 삭제는 마지막 확인을 사용자에게 넘기도록 프롬프트에 규칙을 넣었어요.
- **터미널에 명령을 입력할 수 있어요.** 줄바꿈이 포함된 입력은 곧바로 실행되니, 로봇 제어 터미널이 열려 있을 때는 제안 내용을 꼭 확인하세요.

## 시각화

"이거 시각화해줘", "구조 그려줘", "3D로 보여줘"처럼 요청하면 설명을 읽어주면서 화면 위에 그림을 띄워요.
말로 설명하기 어려운 경우(물체의 3D 구조 등)에는 요청하지 않아도 그릴 수 있어요.

| 종류 | 모습 |
|---|---|
| 3D | 테두리 없는 투명 창에 물체가 떠 있고, 포스트잇 라벨이 지시선으로 붙어요. 드래그로 회전, 휠로 확대 |
| 설명 카드 | 모눈종이 카드에 손글씨, 색 박스, 손으로 그린 화살표 |
| 구조도 | Mermaid 손그림 스타일 (노드 그래프, 상태 머신, 시퀀스) |

- 닫기: 창 오른쪽 위 `× Clear`, 창에서 `Esc`, 또는 답변이 끝난 뒤 `Ctrl+Alt+Backspace`
- 설명은 그림이 다 그려지기 전에 먼저 읽기 시작해요. 그림은 보통 10~30초 걸려요.
- Claude가 만든 JS가 실행되므로 창은 격리돼 있어요: buddy에 같이 들어 있는 three.js·Mermaid·폰트만 불러올 수 있고,
  인터넷·파일·다른 페이지로의 이동·새 창은 모두 막혀 있어요.
- NVIDIA PRIME 노트북(`__NV_PRIME_RENDER_OFFLOAD=1`이 전역 설정된 환경)에서는 WebEngine이 GPU 초기화에 실패해 앱이 죽어요.
  그래서 buddy는 자기 프로세스에서만 PRIME 변수를 빼고 내장 GPU로 그려요. NVIDIA로 그리려면 `.env`에 `BUDDY_VISUAL_GPU=nvidia`.
- 시각화 창은 스크린샷에 포함돼요. 그래서 "이 그림에서 이 부분은 뭐야?"라고 이어서 물을 수 있어요.

## 가리키기 모드 동작

1. Ctrl+Alt만 0.35초 이상 누르면 시작해요. Ctrl+Alt+T 같은 다른 단축키는 그대로 동작해요
   (키를 가로채지 않고 키보드 상태만 읽어요).
2. 커서 밑 요소 찾기는 정확한 순서대로 시도해요:
   - **AT-SPI 접근성**: GTK·Qt 앱, GNOME 상단 바 등은 버튼 하나 단위로 정확한 테두리를 보여줘요.
     Chrome·Slack 같은 Chromium/Electron 앱은 시스템 접근성이 꺼져 있으면 내부 요소를 노출하지 않아요.
   - **창 경계**: 위 방법이 안 되면 커서 밑 창 전체를 테두리로 보여줘요.
   - **Claude 보정**: 답이 오면 Claude가 스크린샷에서 찾은 정확한 경계(`[BOX]`)로 테두리를 좁혀요.
3. 손을 떼면 스크린샷에 커서(빨간 링), 감지한 요소(주황 상자), 움직인 궤적(노란 선)을 표시해서 보내요.
   노치는 열리지만 키보드 포커스는 가져가지 않아요.

Chromium/Electron 앱에서도 처음부터 정확한 테두리를 보고 싶으면 아래 설정을 켜고 해당 앱을 다시 시작하세요.
접근성 트리를 만드느라 앱이 약간 무거워질 수 있어요.
```bash
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

## 구조

```
노치 입력 → buddy 창 숨김 → 모든 화면 캡처(긴 변 1280px JPEG)
  → Claude 스트리밍 응답(노치에 실시간 표시)
  → 끝의 [POINT:x,y:label:screenN] 태그 파싱 → 버디가 베지어 곡선으로 비행
  → Edge TTS로 읽기
```

| 파일 | 역할 |
|---|---|
| `buddy/app.py` | 전체 흐름 제어 |
| `buddy/notch.py` | 상단 노치 창 (접힘/펼침, 입력, 답변) |
| `buddy/overlay.py` | 화면별 투명 클릭통과 창 + 파란 삼각형 버디 애니메이션 |
| `buddy/claude_code_client.py` | 구독 백엔드: `claude -p` 헤드리스 호출 |
| `buddy/claude_client.py` | API 백엔드: Anthropic SDK 스트리밍 |
| `buddy/message_content.py` | 스크린샷+질문 메시지 구성 (두 백엔드 공용) |
| `buddy/prompts.py` | 시스템 프롬프트 |
| `buddy/pointing.py` | `[POINT]` 태그 파싱, 스크린샷 좌표 → 화면 좌표 변환 |
| `buddy/screen_capture.py` | 멀티모니터 캡처 |
| `buddy/speech.py` | Edge TTS 합성 + Qt Multimedia 재생 |
| `buddy/hotkey.py` | XGrabKey 전역 단축키 |
| `buddy/point_mode.py` | Ctrl+Alt 누름 감지 (XQueryKeymap 폴링) |
| `buddy/actions.py` | `<action>{...}</action>` 동작 태그 파싱·검증 |
| `buddy/input_control.py` | XTEST 마우스·키보드 입력, 창 활성화 |
| `buddy/pointer_inspector.py` | 커서 밑 요소 찾기 (AT-SPI → X11 창) |
| `buddy/x11_focus.py` | 노치 입력창 키보드 포커스 (한글 입력기 포함) |
| `buddy/tray.py` | 트레이 아이콘 메뉴 |
| `bin/buddy` | 앱 런처 (venv 실행, 로그, 실패 알림) |
| `packaging/install-desktop.sh` | 앱 목록·자동 실행 등록 |
| `buddy/visual_block.py` | `<visual>` 블록 파싱 |
| `buddy/visual_window.py` | 시각화 창 (Qt WebEngine, `buddy://` 스킴, 외부 요청 차단) |
| `buddy/web/kit/` | 시각화용 JS·CSS 도구 (`scene3d`, `label`, `connect`, `mermaid`) |
| `buddy/web/vendor/` | three.js, Mermaid, 손글씨 폰트 |

## 아직 없는 것

- 음성 입력(STT) — 후순위
- Wayland 지원
