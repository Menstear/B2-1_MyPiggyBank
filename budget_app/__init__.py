"""budget_app: 표준 라이브러리만으로 만든 콘솔 가계부.

계층 구조
---------
- models      : 데이터 구조(dataclass) 정의        [모델]
- repository  : 파일 I/O + 제너레이터 스트리밍      [저장소]
- service     : 비즈니스 로직(검색/요약/예산 등)     [서비스]
- cli         : argparse + 대화형 입력 + 출력        [CLI]
- decorators  : 공통 관심사(예외/로그/시간 측정)
- errors      : 사용자 친화 예외(원인 + 힌트)
- validators  : 입력 검증
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
