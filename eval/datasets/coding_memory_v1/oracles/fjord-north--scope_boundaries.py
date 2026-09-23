"""Immutable oracle for fjord-north:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-fjord-north', service.scope_owner()


if __name__ == "__main__":
    main()
