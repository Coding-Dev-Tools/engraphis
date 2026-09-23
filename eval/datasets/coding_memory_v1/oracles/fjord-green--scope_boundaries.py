"""Immutable oracle for fjord-green:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-fjord-green', service.scope_owner()


if __name__ == "__main__":
    main()
