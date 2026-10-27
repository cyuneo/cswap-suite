# cswap suite

[English](README.md) | **한국어** | [简体中文](README.zh-CN.md)

본인 소유의 Claude Code 계정을 여러 개 쓰는 사람을 위한 로컬 도구 모음입니다. Claude Code에 로그인된 계정을 전환하는 [claude-swap](https://github.com/cyuneo/claude-swap) (`cswap`)을 중심으로 만들었고, 다음 기능을 더합니다.

- **자동 전환.** 현재 계정이 사용량 한도에 가까워지면 cswap이 여유가 가장 많은 계정으로 전환합니다. 계정마다 한도와 사용 시간대를 따로 정할 수 있고, 한 계정을 예비로 남겨 두었다가 다른 계정을 모두 소진했을 때만 쓰게 할 수도 있습니다.
- **VS Code 사이드바.** 모든 계정의 사용량 링, 일시 중지·재개가 가능한 자동 전환 상태, API 워커의 작업과 지출을 보여 줍니다.
- **휴대폰 페이지** (cswap-remote). 본인의 Tailscale 네트워크를 통해 휴대폰에서 세션을 확인하고, 권한 요청에 응답하고, 계정을 전환할 수 있습니다.
- **API 워커** (Claude Code 플러그인). 백그라운드 작업을 Console API 키로 과금되는 워커에 맡기거나, 클라우드 세션을 시작합니다. 켜기/끄기 스위치, 작업별 예산, 월 한도가 있습니다.
- **Agent Monitor** (VS Code 확장, [별도 저장소](https://github.com/cyuneo/cyuneo-agent-monitor)). 모든 세션을 한곳에서 지켜봅니다. 채팅이 사용량 한도에 걸려 멈추면, 한도가 초기화되거나 cswap이 계정을 전환한 뒤 이어서 진행합니다.

모든 것은 본인 컴퓨터에만 남습니다. 설정, 로그인 정보, 로그는 절대 업로드되지 않습니다. 원격 계정 풀은 없으며, 계정은 누구와도 공유되지 않습니다.

## 요구 사항

- Linux. Ubuntu에서 테스트했으며, 백그라운드 서비스는 systemd 사용자 서비스로 동작합니다.
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)와 [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- VS Code: 사이드바와 Agent Monitor에 필요합니다 (선택 사항).

## 설치

[Releases](https://github.com/cyuneo/cswap-suite/releases)에서 `cswap-suite.tar.gz`를 내려받은 뒤 다음을 실행합니다.

```bash
tar xzf cswap-suite.tar.gz
./cswap-suite/install.sh          # 자동 전환을 백그라운드 서비스로 계속 실행하려면 --auto 추가
```

- `~/.local/share/cswap-suite`에 설치되고, 명령은 `~/.local/bin`에 들어갑니다. 압축을 푼 폴더는 설치 후 삭제해도 됩니다.
- 업데이트하려면 새 번들을 같은 방법으로 설치하거나 `~/.local/share/cswap-suite/install.sh`를 실행합니다. 실행 중인 서비스는 새 버전으로 재시작됩니다.
- 옵션: `--auto` (자동 전환 서비스), `--remote` (휴대폰 페이지 서비스), `--no-api-worker`, `--no-vscode`, `--no-monitor`, `--force-vscode` (이미 더 새로운 버전이 설치되어 있어도 번들에 포함된 확장을 설치). `./install.sh --help`로 목록을 볼 수 있습니다.
- 로그아웃한 상태에서도 서비스가 실행되게 하려면 `loginctl enable-linger`를 한 번 실행합니다.

## 계정 추가

- **이 컴퓨터에서:** VS Code의 cswap 사이드바에서 ＋를 누르거나, 터미널에서 Claude Code에 로그인한 뒤 `cswap add`를 실행합니다.
- **직접 로그인하지 않는 다른 컴퓨터에서:** 로그인된 컴퓨터의 계정을 장기 setup 토큰으로 사용하고, 사용량은 SSH로 동기화합니다. `cswap-satellite setup --server <host>`를 실행한 다음 `cswap-satellite install-timer`를 실행합니다. 자세한 내용은 [cswap-satellite/README.md](cswap-satellite/README.md) (중국어)를 참고하세요.

## 이 저장소의 구성

| 폴더 | 설명 |
| --- | --- |
| [cswap-status-vscode/](cswap-status-vscode/) | VS Code 확장: cswap 사이드바와 상태 표시줄 |
| [api-worker/](api-worker/) | 본인 API 키 또는 클라우드에서 백그라운드 작업을 실행하는 Claude Code 플러그인 |
| [cswap-remote/](cswap-remote/) | 휴대폰 페이지 |
| [cswap-satellite/](cswap-satellite/) | setup 토큰으로 다른 컴퓨터에서 계정 사용 |
| [cswap-hooks/](cswap-hooks/) | 전환할 때마다 그 뒤에 실행되는 작업: reasoning effort를 그대로 유지하고 로그 한 줄을 기록 |
| [suite/](suite/) | 설치 프로그램, 번들 빌더, 테스트 |

관련 저장소:

| 저장소 | 설명 |
| --- | --- |
| [cyuneo/claude-swap](https://github.com/cyuneo/claude-swap) | 이 프로젝트의 cswap: [realiti4/claude-swap](https://github.com/realiti4/claude-swap)의 포크로, 계정별 한도, 자동 전환 상태와 일시 중지, 전환 훅을 추가 |
| [cyuneo/cyuneo-agent-monitor](https://github.com/cyuneo/cyuneo-agent-monitor) | Agent Monitor |

각 구성 요소의 상세 문서는 해당 폴더에 중국어로 작성되어 있습니다. 유지 관리자용 메모는 [docs/MAINTAINING.md](docs/MAINTAINING.md)와 [docs/PUBLISHING.md](docs/PUBLISHING.md)에 있습니다.

## 라이선스

[PolyForm Noncommercial 1.0.0](LICENSE): 사용, 수정, 공유할 수 있지만 상업적 목적으로는 할 수 없습니다. 서드파티 프로젝트와 그 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.

## 감사의 말

- Onur Cetinkol의 [claude-swap](https://github.com/realiti4/claude-swap) (MIT): 이 프로젝트의 cswap은 이를 수정한 버전이며, 원래 라이선스를 따릅니다.
- 코드 작성과 리뷰에 Claude Code의 도움을 받았습니다.

Claude와 Claude Code는 Anthropic의 제품입니다. 이 프로젝트는 이들과 함께 동작하는 독립적인 개인 프로젝트이며, Anthropic과 제휴 관계가 없습니다.
