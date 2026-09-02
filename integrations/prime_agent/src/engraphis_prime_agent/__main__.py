"""Allow ``python -m engraphis_prime_agent``."""
from .cli import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
