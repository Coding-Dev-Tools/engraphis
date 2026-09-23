"""Immutable oracle for fjord-violet:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-fjord-violet', service.scope_owner()


if __name__ == "__main__":
    main()
