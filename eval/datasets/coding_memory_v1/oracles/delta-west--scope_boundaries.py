"""Immutable oracle for delta-west:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-delta-west', service.scope_owner()


if __name__ == "__main__":
    main()
