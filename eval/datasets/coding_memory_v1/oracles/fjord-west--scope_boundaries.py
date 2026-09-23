"""Immutable oracle for fjord-west:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-fjord-west', service.scope_owner()


if __name__ == "__main__":
    main()
