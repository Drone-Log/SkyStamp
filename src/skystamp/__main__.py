import sys

if len(sys.argv) > 1 and sys.argv[1].startswith("-") or len(sys.argv) > 2:
    from .cli import main
else:
    from .gui import main

main()
