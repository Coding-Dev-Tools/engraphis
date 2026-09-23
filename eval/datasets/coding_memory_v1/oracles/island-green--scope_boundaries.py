"""Immutable oracle for island-green:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-island-green', service.scope_owner()


if __name__ == "__main__":
    main()
