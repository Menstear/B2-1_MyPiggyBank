"""진입점 — `python -m budget_app <command> [options]`.

argparse 로 인자를 파싱하고 해당 명령 핸들러로 분기한다.
로그는 stderr로 보내며, --verbose 를 주면 DEBUG 레벨까지 출력한다.
정상 종료는 0, 오류 종료는 0이 아닌 값(각 예외의 exit_code).
"""

from __future__ import annotations

import logging
import sys

from .cli import build_parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
    logging.basicConfig(level=level, format="[log] %(message)s", stream=sys.stderr)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
