"""Immutable oracle for delta-violet:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-delta-violet', service.scope_owner()


if __name__ == "__main__":
    main()
