#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hamsa.settings")
    from django.core.management import execute_from_command_line

    # Serve on port 5000 by default (`runserver` alone would use 8000).
    if sys.argv[1:2] == ["runserver"] and not any(not a.startswith("-") for a in sys.argv[2:]):
        sys.argv.append("5000")
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
