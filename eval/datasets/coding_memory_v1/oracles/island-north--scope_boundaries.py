"""Immutable oracle for island-north:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-island-north', service.scope_owner()


if __name__ == "__main__":
    main()
