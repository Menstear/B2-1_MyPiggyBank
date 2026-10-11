# budget_app — 콘솔 가계부

표준 라이브러리만으로 만든 파일 기반 콘솔 가계부입니다.
수입/지출 내역의 추가·조회·검색·수정·삭제, 월별 요약, 예산 경고, 카테고리 관리,
CSV 가져오기/내보내기를 지원합니다.

- **제너레이터 스트리밍**: 저장 파일을 한 번에 로드하지 않고 한 줄씩 처리
- **데코레이터**: 예외 처리 / 실행 로그 / 실행 시간 측정을 공통 분리
- **타입 힌트**: 함수·데이터 구조의 계약을 명시
- **모듈화**: 모델 / 저장소 / 서비스 / CLI 계층 분리
- **원자적 저장**: update/delete 시 임시 파일 작성 후 `os.replace` 로 교체

## 요구 환경

- Python 3.10 이상 (외부 패키지 불필요, 표준 라이브러리만 사용)

## 실행 방법

프로젝트 루트(이 README가 있는 폴더)에서 모듈로 실행합니다.

```bash
python -m budget_app <command> [options]
```

모든 명령은 `--help` 로 사용법을 볼 수 있습니다.

```bash
python -m budget_app --help
python -m budget_app add --help
python -m budget_app summary --help
```

- 공통 옵션: `--data-dir <폴더>` (저장 위치 변경), `--verbose` (실행 로그/시간 출력)
- 공통 옵션은 명령 앞이나 뒤, 하위 명령 앞이나 뒤에 사용할 수 있습니다. 같은 옵션을 여러 번 지정하면 마지막 값이 적용됩니다.
- 정상 종료 코드는 `0`, 오류 종료 코드는 `0`이 아닌 값입니다.
- 오류는 스택트레이스 대신 `[오류] 원인` + `[힌트] 해결 방법` 형태로 출력됩니다.

## 저장 파일 위치 / 형식

- 기본 저장 폴더: `./data` (`--data-dir` 로 변경 가능, 없으면 자동 생성)
- 형식: **JSONL** (한 줄에 JSON 객체 1개), UTF-8
- 파일은 3개로 분리:

| 파일 | 내용 | 레코드 예시 |
| --- | --- | --- |
| `transactions.jsonl` | 거래 내역 | `{"id": "TX-000001", "type": "expense", "date": "2024-01-15", "amount": 15000, "category": "food", "memo": "점심", "tags": ["meal"]}` |
| `categories.jsonl` | 카테고리 | `{"name": "food"}` |
| `budgets.jsonl` | 월 예산 | `{"month": "2024-01", "amount": 500000}` |

- 실행 시 카테고리 파일이 없거나 비어 있으면 기본 카테고리(`food, transport, rent, salary, etc`)가 자동 생성됩니다. 등록된 카테고리가 있으면 그대로 유지합니다.

## 데이터 모델

- `Transaction`: `id`, `type`(income/expense), `date`(YYYY-MM-DD), `amount`(양수 정수), `category`, `memo`(선택), `tags`(선택)
- `Category`: `name`
- `Budget`: `month`(YYYY-MM), `amount`(양수 정수)

## 주요 명령 예시

### add — 거래 추가 (대화형)

```bash
$ python -m budget_app add
날짜(YYYY-MM-DD): 2024-01-15
타입(income/expense): expense
카테고리: food
금액(양수): 15000
메모(선택): 점심
태그(쉼표로 구분, 없으면 엔터): meal
[저장 완료] id=TX-000001
```

- 날짜/타입/금액 형식 오류, 미등록 카테고리는 **재입력**을 요구합니다.

### list — 거래 목록 (거래 날짜 최신순, 스트리밍)

```bash
$ python -m budget_app list --limit 3
TX-000012 | 2024-01-15 | expense | food | 15000 | 점심
TX-000011 | 2024-01-14 | income | salary | 3000000 |
TX-000010 | 2024-01-12 | expense | transport | 20000 |
```

- `--limit N` (기본 20). 거래 날짜 내림차순이며, 같은 날짜는 거래 ID의 숫자 부분이 큰 순서입니다. `search`도 같은 기준을 사용합니다.
- 파일을 한 줄씩 읽고 힙에 최대 N건만 유지하므로, 과거 거래를 나중에 추가하거나 날짜를 수정해도 최신 N건을 조회합니다.

### search — 거래 검색 (최신순)

```bash
python -m budget_app search --from 2024-01-01 --to 2024-01-31 \
    --category food --type expense --q 점심 --tag meal --limit 10
```

- 조건: `--from`, `--to`, `--category`, `--type`, `--q`(메모 키워드), `--tag`
- `--limit N`을 지정하면 최대 N건만 메모리에 유지합니다. 제한이 없으면 임시 JSONL 파일로 나눠 정렬한 뒤 병합하며 결과를 한 건씩 출력합니다. 임시 파일은 조회 완료 또는 중단 시 정리됩니다.
- `list`와 `search`의 `--limit 0`은 빈 결과를 반환하고, 음수는 입력 오류로 처리합니다.

### summary — 월별 요약 + 예산

```bash
$ python -m budget_app budget set --month 2024-01 --amount 500000
[저장 완료] 2024-01 예산 500000원

$ python -m budget_app summary --month 2024-01 --top 3
총 수입: 3000000원
총 지출: 215000원
잔액: 2785000원
예산: 500000원 (사용률 43.0%)

지출 TOP 3
1) rent 150000원
2) food 45000원
3) transport 20000원
```

- 예산을 초과하면 `[경고] 예산을 N원 초과했습니다!` 가 추가로 출력됩니다.
- 데이터가 없는 달은 `YYYY-MM 데이터 없음`과 0원 합계를 출력하며, 설정된 예산이 있으면 사용률 0%도 표시합니다.

### category — 카테고리 관리

```bash
python -m budget_app category list
python -m budget_app category add food        # 생략 시 대화형 입력
python -m budget_app category remove food                 # 사용 중이면 삭제 거부
python -m budget_app category remove food --replace etc   # 사용 내역을 etc로 재분류 후 삭제
```

### update — 거래 수정 (옵션 기반으로 고정)

```bash
python -m budget_app update --id TX-000001 \
    [--date 2024-01-16] [--type expense] [--category food] \
    [--amount 18000] [--memo "메모"] [--tags "a,b"]
```

- 지정한 필드만 수정합니다. 없는 id는 실패 메시지와 함께 오류 종료합니다.

### delete — 거래 삭제

```bash
python -m budget_app delete --id TX-000001
```

- 없는 id는 `[오류] 거래를 찾을 수 없습니다` 와 함께 오류 종료합니다.

### export / import — CSV 내보내기/가져오기

```bash
# export 는 --month 또는 --from/--to 중 하나 이상이 필수
python -m budget_app export --out export.csv --month 2024-01
python -m budget_app export --out export.csv --from 2024-01-01 --to 2024-01-31

python -m budget_app import --from import.csv
```

출력 예시:

```
[완료] export.csv (12 records)
[완료] imported=5, skipped=0
```

- import 시 형식 오류/미등록 카테고리 행은 건너뛰고 `skipped` 로 집계합니다.
- CSV의 따옴표 구조나 인코딩이 잘못됐거나 파일 읽기/저장에 실패하면 거래 파일 전체를 그대로 유지합니다. 정상 행을 임시 파일에 준비하고 가져오기를 끝까지 처리한 뒤 한 번에 반영합니다.
- 긴 메모와 따옴표, 쉼표, 줄바꿈도 CSV로 내보내고 다시 가져올 수 있습니다.
- export의 출력 경로로 내부 저장 파일이나 해당 파일의 링크를 지정할 수 없습니다. 출력에 실패하면 기존 CSV도 유지합니다.

## import/export CSV 스키마

- 공통: **UTF-8, 헤더 포함**

| column | required | 설명 |
| --- | --- | --- |
| date | Y | YYYY-MM-DD |
| type | Y | income / expense |
| category | Y | 등록된 카테고리 |
| amount | Y | 양수 정수 |
| memo | N | 문자열 |
| tags | N | 쉼표(,) 구분 문자열 |

예시:

```csv
date,type,category,amount,memo,tags
2024-02-01,expense,food,8000,커피,cafe
2024-02-02,income,salary,3000000,,
```

## 프로젝트 구조

```
.
├── README.md
├── budget_app/
│   ├── __main__.py     # 진입점: 인자 파싱 → 명령 분기, 종료 코드
│   ├── cli.py          # [CLI]    argparse + 대화형 입력 + 출력
│   ├── service.py      # [서비스] 검색/요약/예산/카테고리/CSV 로직
│   ├── ordering.py     # 날짜순 정렬: 제한 조회 힙 / 전체 조회 임시 JSONL 병합
│   ├── repository.py   # [저장소] JSONL 파일 I/O + 제너레이터 + 원자적 교체
│   ├── models.py       # [모델]   Transaction / Category / Budget (dataclass)
│   ├── validators.py   # 입력 검증 (날짜/금액/타입/월/태그)
│   ├── decorators.py   # 예외 처리 / 실행 로그 / 실행 시간 측정
│   └── errors.py       # 원인 + 힌트를 갖는 예외 계층, 종료 코드
├── tests/              # unittest 기반 테스트 (tempfile 격리)
│   ├── test_validators.py   # 날짜/월 정규화, 금액·타입 검증, 태그 파싱
│   ├── test_repository.py   # 스트리밍, 원자적 update/delete, 저장소
│   ├── test_service.py      # CRUD/검색/요약/예산/카테고리/CSV 왕복
│   ├── test_cli.py          # 종료 코드, 대화형 add (subprocess)
│   └── test_ordering.py     # 날짜 정렬, 메모리/파일 핸들 제한, 임시 파일 정리
└── data/               # 최초 실행 시 자동 생성 (저장 파일 3개)
```

## 테스트

표준 라이브러리 `unittest` 로 계층별 테스트를 제공합니다(외부 패키지 불필요).
각 테스트는 `tempfile` 로 격리된 임시 폴더를 사용하므로 `./data` 를 건드리지 않습니다.

```bash
python -m unittest discover -s tests        # 전체 실행
python -m unittest discover -s tests -v     # 상세 출력
python -m unittest tests.test_service       # 특정 모듈만
```

- `test_validators.py` : 날짜/월 정규화, 금액·타입 검증, 태그 파싱
- `test_repository.py` : 스트리밍, 원자적 update/delete, 카테고리/예산 저장소
- `test_service.py`    : add/list/search/summary/budget/category/update/delete, CSV 왕복
- `test_cli.py`        : 종료 코드, 대화형 add 흐름(subprocess)
- 추가 회귀 테스트: 공통 옵션 위치, 날짜 수정/과거 거래 import 후 정렬, 빈 카테고리 초기화, 빈 달의 예산 표시, 저장 파일 덮어쓰기 차단, 긴 CSV 문자열, 입출력 실패 시 원본 보존
- `test_ordering.py`   : 제한 조회의 메모리 사용, 전체 조회의 다단계 병합, 동일 날짜의 숫자 ID 정렬, 조회 완료/중단/오류 시 임시 파일 정리

## 설계 메모

- **스트리밍**: 조회는 `for line in f: yield json.loads(line)` 로 파일을 한 줄씩 처리합니다.
  제한 조회는 힙으로 최신 N건만 유지하고, 전체 검색은 작은 묶음의 외부 정렬과 병합을 사용해 거래 전체를 메모리에 올리지 않습니다. 서비스의 `search`는 제너레이터를 반환합니다.
- **데코레이터**: `@handle_errors`(원인+힌트 출력/종료 코드), `@log_execution`, `@measure_time`
  를 모든 명령 핸들러에 적용해 공통 관심사를 분리했습니다. (`--verbose` 로 로그 확인)
- **원자적 저장**: update/delete/카테고리 재분류는 임시 파일에 먼저 쓴 뒤 `os.replace` 로 교체하여
  쓰기 도중 실패해도 원본 데이터가 손상되지 않습니다.
- **추가와 가져오기 실패**: 한 건 추가 시 쓰기가 실패하면 기존 파일 길이로 복구합니다. CSV 가져오기는 정상 행들을 스트리밍하며 임시 파일에 준비해 전체 처리 성공 후 교체합니다.
- **종료 코드**: 성공 `0`, 검증 오류 `2`, 없는 데이터 `3`, 카테고리 오류 `4`,
  저장소 오류 `5`, 입출력 오류 `6`.
