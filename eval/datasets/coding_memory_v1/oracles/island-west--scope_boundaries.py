"""Immutable oracle for island-west:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-island-west', service.scope_owner()


if __name__ == "__main__":
    main()
