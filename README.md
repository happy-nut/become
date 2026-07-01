# study-template

**무엇이든** 라이트너 박스로 공부하는 범용 학습 레포 템플릿.
AI 코딩 에이전트가 학습 코치로 직접 동작한다. 별도 프로그램·노트 준비가 필요 없다.

토익, 자격증, 면접, 역사, 의학 — 주제만 다를 뿐 학습법은 그대로다.

## 어떤 에이전트에서든 동작한다

학습 지침의 정본은 **`AGENTS.md`** 한 파일이다. 주요 AI 코딩 도구가 이 파일을 인식한다:

| 에이전트 | 읽는 파일 | 처리 |
|---|---|---|
| OpenAI Codex · Cursor · GitHub Copilot 등 | `AGENTS.md` | 네이티브로 읽음 |
| Claude Code | `CLAUDE.md` → `@AGENTS.md` import | 자동 |
| Gemini CLI | `GEMINI.md` → `AGENTS.md` 읽으라 지시 | 자동 |

`CLAUDE.md`·`GEMINI.md`는 얇은 포인터일 뿐이다. **지침을 고칠 땐 `AGENTS.md`만 고친다.**

## 어떻게 쓰나

1. **이 레포를 템플릿으로 새 레포 생성** — GitHub "Use this template" 버튼, 또는
   `gh repo create my-study --template happy-nut/study-template --private`
2. 새 레포에서 AI 코딩 에이전트(Claude Code · Codex · Cursor · Gemini CLI 등) 실행 → **"공부 시작하자"**
3. 코치가 무엇을 공부할지 짧게 인터뷰하고 `PROFILE.md`를 채운다 (딱 한 번)
4. 이후 묻고 막히는 항목이 자동으로 카드가 되어 쌓이고, 라이트너 간격으로 다시 출제된다

## 구조

| 파일 | 역할 | 바뀌나? |
|------|------|---------|
| `AGENTS.md` | 학습 엔진 정본 — 박스·왜?체인/재인드릴·망각곡선 규칙 | ❌ 절대 안 바뀜 |
| `CLAUDE.md` / `GEMINI.md` | 각 에이전트 진입점 (AGENTS.md 포인터) | ❌ |
| `PROFILE.md` | 무엇을 공부하는가 (주제·목표·출제 범위) | ✅ 유일하게 바뀌는 파일 |
| `state.tsv` | 카드별 통계 (박스 위치는 디렉토리가 진실) | 자동 |
| `box1`~`box4` | 라이트너 박스 | 자동 |

## 템플릿에 새 기능이 추가되면 (파생 레포 업데이트)

GitHub 템플릿은 fork가 아니라서 **자동 동기화 연결이 없다.** 엔진과 사용자 데이터가 파일 단위로
분리돼 있으므로, 파생 레포는 엔진 파일만 골라 최신본을 받으면 카드·프로필을 그대로 둔 채 업데이트된다.

**방법 A — 코치에게 시키기 (가장 간단)**
파생 레포에서 `"엔진 업데이트"` 라고 하면, 코치가 이 공개 템플릿에서 최신 엔진을 받아 `AGENTS.md`·
`CLAUDE.md`·`GEMINI.md`만 덮어쓰고 버전 변경 요약을 알려준다. (`PROFILE.md`·`box*`·`state.tsv`은 안 건드림)

**방법 B — git remote**
```
git remote add template https://github.com/happy-nut/study-template.git
git fetch template
git checkout template/master -- AGENTS.md CLAUDE.md GEMINI.md
```

엔진에는 `AGENTS.md` 맨 위에 `<!-- 엔진 버전: X -->` 가 있어 현재/최신 버전을 비교할 수 있다.

## 특징

- **학습 유형 자동 판단** — 암기형(재인 드릴) / 이해형(왜? 체인) / 혼합형을 **카드마다 코치가 스스로** 결정한다. 사용자가 고르지 않는다.
- **망각 곡선 반영** — 자주 틀린 카드는 더 자주, 오래 방치된 카드는 더 먼저 출제.
- **자동 카드화** — 노트를 미리 만들지 않는다. 대화에서 카드가 쌓인다.
