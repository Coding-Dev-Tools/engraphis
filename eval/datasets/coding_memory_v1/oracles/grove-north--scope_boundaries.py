"""Immutable oracle for grove-north:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-grove-north', service.scope_owner()


if __name__ == "__main__":
    main()
