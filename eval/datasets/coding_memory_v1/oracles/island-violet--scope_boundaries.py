"""Immutable oracle for island-violet:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-island-violet', service.scope_owner()


if __name__ == "__main__":
    main()
