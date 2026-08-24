# become

*[English](README.md) · 한국어*

**내 손안의 대학.** become은 한 명의 조정자와 다섯 전문 에이전트 계약, 수행 근거 기반 개인 경로,
시간 기반 기억 복습으로 이루어진 로컬 퍼스트 학습 시스템입니다. 역할 모델은
[ALTER 학습 흐름 영상](https://www.youtube.com/watch?v=lF8_DX2NxjI)에 나온 책임을 실제 상태와 실패
조건으로 구현했고, 데이터와 강제 계층은 작고 로컬하게 유지합니다.

```text
Orchestrator  workflow 순서·의존성·중단과 재개
├─ Advisor    도착점·baseline·순서·제외 범위·증명 milestone
├─ Librarian  자료 판정과 강한 3~4개 source shelf
├─ Tutor      교육·혼동 진단·개념 연결·지연 인출
├─ Editor     버전별 교정과 학습자 재작성 루프
└─ Roommate   의도적으로 먼 분야를 통한 연결 질문
```

`AGENTS.md`는 조정자 계약, `agents/*.md`는 전문 역할의 독립 책임과 권한, `become.py`는 산출물 저장과
전이 검증을 담당합니다. Python 표준 라이브러리만 사용합니다. 데스크톱에서는 현재 에이전트 호스트를,
모바일에서는 `mobile.py`가 격리해 호출하는 Codex CLI를 사용해 실제 전문 컨텍스트를 실행합니다.

## 시작하기

Orchestrator에게 학습 요청을 하거나 로컬에서 첫 경로를 확인합니다.

```bash
python3 become.py --actor orchestrator orchestrator route --intent learn
python3 become.py --actor orchestrator orchestrator workflow-start --intent learn --request "운영 백프레셔 학습"
```

현재 상태에 필요한 역할만 실행합니다. 새 학습자는 보통 `Advisor → Librarian → Tutor → Advisor`,
준비된 shelf가 있으면 Tutor부터 시작합니다. Editor와 Roommate는 결과물 검토나 외부 관점이 실제로
필요할 때만 호출합니다.

## 각 역할이 실제로 하는 일

Advisor는 한 번에 결정 하나를 인터뷰하며 커리큘럼 확정 전 다섯 결정을 반드시 해결합니다. 도착점,
수행 근거가 있는 baseline, 선수 관계 순서, cut list, 학습자 결과물로 증명하는 milestone입니다.
Tutor 관찰로 이 경로를 갱신하고, 예전에 해결한 혼동도 예상 기억률이 낮아지면 remedial 목표로 되살립니다.
모든 sequence step에는 증명 milestone이 필요하며 현재 active step만 완료할 수 있습니다. 현재 curriculum
version의 증거가 통과해야 다음 prerequisite-ready step이 열립니다.
학습 목표나 핵심 focus가 바뀌면 이전 profile·curriculum을 history에 보존하고 해당 지식의 자동 복습을
비활성화한 뒤 다섯 결정을 새로 시작합니다.

Librarian은 원문을 열고 접근 가능성과 내용 검증을 구분합니다. 모든 후보를 관련성, 신뢰성, 현재 수준
적합성, signal 밀도, 1~5 priority로 판단하고 curriculum version·step에 묶습니다. 검증·판정된 signal만
shelf에 들어가며 빈 원문과 현재 수준보다 너무 쉽거나 어려운 자료를 제외하고 active 단계에서 가장 강한
서로 다른 원문 3~4개를 고릅니다. 내용 지문이 같은 복사본은 한 자료로 세며, 사용 직전에 접근성과 전체
내용 지문을 다시 확인합니다. Tutor는 오래된 version, 미래 step, 사라지거나 바뀐 원문, 미선택 자료를
근거로 사용할 수 없습니다.

Tutor는 새 내용을 시험하기 전에 먼저 가르칩니다. 처음 헷갈린 지점을 포착하고 `왜 쓰는가`, `왜 이렇게
되었는가`, `왜 이 결과가 나오는가`, `그래서 어디에 쓰는가`를 모두 설명합니다. 연결 기준은 현재 전공의
active related knowledge 하나 또는 Advisor가 확정한 curriculum baseline 하나입니다. 실제 질문·학습자 원답·
판정 근거·확신도·약점과 양방향 개념 연결을 저장합니다. 전공이 바뀌면 제목이 같은 개념도 기억·혼동을
섞지 않고 새 지식으로 저장합니다. 새 학습 workflow는 claim 뒤 `teach → 별개 사례 application review`,
만기 복습은 `힌트 없는 retrieval review → 답에서 드러난 빈틈을 왜/연결 설명으로 teach`해야 끝납니다.
새 지식의 망각 시간은 teach 시점이 아니라 첫 application review가 끝난 시점부터 셉니다.

Editor는 목적·독자와 학습자 버전을 저장합니다. thinking, logic, evidence, repetition, structure,
precision, accuracy 일곱 축을 모두 검토하고, 각 finding에 문제 구간·진단·학습자가 할 행동을 남깁니다.
학습자가 새 버전을 내면 다시 검토해 이전 finding을 resolved로 닫고, 해결됐다 다시 나타난 축은
regressed 이력으로 연결합니다. 열린 finding이나
실패한 milestone criterion이 있으면 pass할 수 없고 `previous_versions`가 감사 이력으로 남습니다.
공백·Unicode 표기만 바꾼 동일 본문은 새 revision이 아닙니다.
작성자 근거가 없는 v3 결과물은 `legacy_unknown`으로 격리하며, 학습자가 다시 제출하기 전에는 검토하거나
milestone 증거로 쓸 수 없습니다.

Roommate는 세션 체크포인트가 아닙니다. 현재 전공과 다른 분야의 구체적 작동 원리를 가져와 연결 질문
하나를 던지고 학습자의 답을 기다립니다. 연결 mapping과 비유가 깨지는 지점까지 저장하며, 억지 연결은
`no_connection`, 확인이 필요하면 `needs_verification`으로 끝낼 수 있습니다.
답하지 않은 perspective는 한 번에 하나만 존재할 수 있고, 같은 lens·질문 조합을 반복할 수 없습니다.

Orchestrator는 workflow 순서와 세션 연속성을 소유합니다. 의존 step은 앞 작업이 끝나기 전에 claim할
수 없고, summary나 무관한 과거 id로 완료할 수도 없습니다. step 시작 뒤 새로 만들거나 갱신한 expected
output이 있어야 하며, 완료 결과는 다음 step context에 자동 전달됩니다.
조정자가 필요한 이유는 직접 가르치기 위해서가 아니라 계획·조사·교육·편집·관점의 책임을 섞지 않고
완료 조건과 순서를 강제하기 위해서입니다. workflow id에 연결한 session은 재개할 때 live workflow,
현재 step, handoff를 함께 돌려줍니다. 수동 handoff도 같은 완료 조건과 claim 시점 snapshot을 사용하고,
같은 의미 버전의 산출물 하나로 두 요청을 완료할 수 없습니다. 목표 변경을 수행하는 현재 plan은 새
goal·focus에 다시 묶이고, 그 밖의 오래된 workflow/handoff는 `superseded`/`cancelled`가 됩니다.
Tutor step은 관찰·추천을 남기고 후속 Advisor는 그 관찰을 수준 근거로 기록한 뒤 같은 knowledge id에
연결된 목표를 남겨야 합니다. 수준 근거는 원본 Tutor handoff와 생산 Advisor handoff에 함께 귀속되며,
같은 Tutor observation을 다른 Advisor 갱신에서 다시 소비할 수 없습니다. 보존율 변경도 인출 모드를
바꾸므로 진행 중 흐름을 새로 라우팅합니다.
모든 전문 역할 쓰기는 claim된 handoff 안에서만 가능합니다. 서로 다른 역할은 병렬로 움직일 수 있지만
같은 역할은 진행 중 handoff를 하나만 가지며, resource에는 생산한 handoff id가 기록됩니다.
`--scope-handoff`로 현재 작업을 명시적으로 고정할 수 있습니다. 수동 Advisor 작업은 `curriculum` 또는 `advisor_update` 산출물
종류를 명시하며 Editor artifact는 dispatch 때 고정됩니다. Roommate는 현재 분야·문제만 받고 외부 분야·
lens·질문을 직접 만들며, 생성한 perspective는 claim한 handoff에 귀속되어 바꿔치기할 수 없습니다.

## 주요 명령

전문 역할의 쓰기 명령은 Orchestrator가 dispatch한 handoff를 해당 역할이 claim한 뒤 실행합니다.

```bash
python3 become.py --actor advisor advisor interview --decision destination --question "무엇을 할 수 있어야 합니까?" --answer "부하 근거로 큐 용량을 방어한다"
python3 become.py --actor advisor advisor curriculum --spec '{...}'
python3 become.py --actor advisor advisor recommend  # 읽기 전용: 상태를 바꾸지 않음
python3 become.py --actor advisor advisor next       # 쓰기: claim된 handoff에서 remedial 목표 생성·갱신
python3 become.py --actor librarian librarian add --title "공식 문서" --source "/path/to/source" --evidence "직접 읽은 범위"
python3 become.py --actor librarian librarian curate MATERIAL_ID --assessment '{...}'
python3 become.py --actor librarian librarian shelf --curriculum-id CURRICULUM_ID --step-id STEP_ID --candidate-id MATERIAL_1 --candidate-id MATERIAL_2 --candidate-id MATERIAL_3
python3 become.py --actor tutor tutor teach KNOWLEDGE_ID --explanation "왜 쓰는가: ... 왜 이렇게 되었는가: ... 왜 이 결과가 나오는가: ... 그래서 어디에 쓰는가: ..." --connection "저장된 related 지식 또는 curriculum baseline"
python3 become.py --actor tutor tutor review KNOWLEDGE_ID good --confidence complete --prompt "질문" --answer "학습자 원답" --rationale "판정 근거"
python3 become.py --actor learner editor add --title "결과물" --content "초안" --purpose "목적" --audience "독자"
python3 become.py --actor editor editor review ARTIFACT_ID --criteria '{...}' --milestone-criteria '{...}' --verdict revise --next "학습자 수정 행동"
python3 become.py --actor learner editor revise ARTIFACT_ID --content "학습자 수정본"
python3 become.py --actor roommate roommate ask --current-field "분산 시스템" --problem "백프레셔" --outside-field "도시 교통" --lens "진입 램프" --question "어디서 진입을 제한할까?"
python3 become.py --actor orchestrator orchestrator session-start --context "출근길" --workflow-id WORKFLOW_ID
python3 become.py --actor orchestrator orchestrator session-resume
python3 become.py --actor orchestrator orchestrator dispatch --to advisor --task "경로 갱신" --output-kind advisor_update
python3 become.py --actor orchestrator orchestrator dispatch --to editor --task "현재 글 검토" --resource-id ARTIFACT_ID
python3 become.py --actor orchestrator orchestrator workflow-start --intent perspective --request "외부 관점" --current-field "분산 시스템" --problem "백프레셔"
```

전체 명령은 `python3 become.py --help`와 각 역할의 `--help`에서 확인합니다.

## 기억과 근거

개인 상태는 `.become/state.json`, 학습 감사 이벤트는 `.become/reviews.jsonl`에 저장됩니다. `.become/`은
Git에서 제외됩니다. 모든 CLI·PWA 쓰기는 같은 프로세스 간 잠금과 비교 후 저장을 사용하며, 두 파일을 함께
바꾸는 동안 강제 종료되면 로컬 트랜잭션 저널로 이전의 일관된 쌍을 자동 복구합니다. 다른 비공개 위치는
`BECOME_HOME`으로 지정합니다.
v3 상태는 출처를 꾸며내지 않는 보수적 마이그레이션을 하며, 이미 v4인 nested 역할·workflow 손상은
조용히 덮지 않고 거부합니다.

```text
예상 기억률 = 0.9 ^ (경과 일수 / 안정도 일수)
```

`due_at` 전 상호작용은 `exposure`라서 안정도를 올리지 않습니다. 간격 뒤 독립 답변만 `retrieval`입니다.
부분 확신이나 실패에는 구체적인 약점이 필요하며, 해결된 혼동도 history에 남아 나중에 다시 목표가 됩니다.

## 모바일 개인 대학

```bash
python3 mobile.py --home .become
```

`http://127.0.0.1:8765`를 열면 복습과 ALTER 에이전트 대화를 한 화면에서 이어갑니다. 메시지는 즉시
작업 id를 돌려주고 서버의 단일 큐에서 실행되므로 1–3분 걸리는 역할도 페이지를 닫았다 다시 열어 상태와
실제 진행 이벤트를 복구할 수 있습니다. Codex는 매 작업마다 임시 workspace의 상태 복제본만 보며,
검증된 `state.json`과 새 review event만 동시 변경 충돌이 없을 때 하나의 복구 가능한 커밋으로 가져옵니다.
실행 중 사용자의 복습이나 CLI 변경이 있으면 에이전트 결과를 버리고 사용자 상태를 보존합니다.

모바일은 **오늘·커리큘럼·기록·나의 대학** 네 영역으로 나뉩니다. 오늘은 첫 화면에 5/10/15분짜리 핵심
행동 하나만 둡니다. 새 학습에서는 Tutor의 네 가지 왜, 알고 있는 개념과의 연결, 적용 예를 먼저 보여준
뒤 다른 사례에 적용하게 합니다. 만기 인출은 별도 화면 상태이며, 학습자가 답을 제출하거나 명시적으로
포기하기 전에는 기준 설명을 응답이나 DOM에 넣지 않습니다. 피드백은 제출 답을 잠그고 저장된 취약 지점,
그 지점에 필요한 교정, 정확한 다음 시각을 보여줍니다. 커리큘럼에서는 최종 수행 능력, 현재·완료·잠금
단계와 잠금 이유, 수행 증거, 제외 이유를 확인합니다. 기록은 페이지 단위로 읽고, 나의 대학에서는
학습자가 쓴 Editor 결과물과 수정 버전, Roommate 연결과 비유의 한계, 내부 handoff 로그를 숨긴 학습 지원
상태를 다룹니다.

앱 셸과 정제된 현재 강의·경로·기록·대학 사본은 오프라인에서도 읽을 수 있습니다. 초안과 제한된 답안
대기열만 브라우저에 임시 저장하고, 정본은 계속 `state.json`과 `reviews.jsonl`입니다. 각 답안은 안정적인
요청 id와 지식 상호작용 순서를 갖습니다. 재연결 때 같은 id는 원래 receipt를 돌려주므로 중복 기록되지
않고, 오래된 답은 충돌로 남으며, 서버가 단 하나의 review event를 확인한 뒤에만 대기열에서 제거됩니다.
API와 숨겨진 인출 설명은 서비스 워커가 캐시하지 않습니다. 새 셸은 사용자가 안전한 새로고침을 선택한
뒤에만 활성화됩니다.

기본 loopback은 토큰 없이 동작합니다. 원격 접속은 HTTPS 또는 인증된 private tunnel 뒤에서
`--require-token`을 사용하고 `BECOME_MOBILE_TOKEN`을 설정합니다. non-loopback 평문 HTTP는 기본 거부하며,
`--allow-insecure-http`는 Bearer가 암호화를 제공하지 않는다는 위험을 명시적으로 감수할 때만 씁니다.

## 검증

```bash
python3 -m py_compile become.py mobile.py
python3 -m unittest -v
```

역할별 성공과 잘못된 전이, 권한 거부, workflow 의존성, 기존 상태 마이그레이션, 자료 gating, 기억 시간,
로컬 PWA, 여섯 역할 전체 여정을 검증합니다.

## 구조

```text
become.py       로컬 실행·상태 엔진
mobile.py       격리된 Codex 실행·작업 큐·인증을 포함한 모바일 API 서버
mobile/         설치 가능한 네 영역 개인 대학 PWA
AGENTS.md       Orchestrator 계약
agents/         다섯 전문 역할 계약
tests/          역할·마이그레이션·전체 여정 검증
.become/        Git 비추적 개인 데이터
```
