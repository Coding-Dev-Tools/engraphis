"""Immutable oracle for atlas-violet:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-atlas-violet', service.scope_owner()


if __name__ == "__main__":
    main()
